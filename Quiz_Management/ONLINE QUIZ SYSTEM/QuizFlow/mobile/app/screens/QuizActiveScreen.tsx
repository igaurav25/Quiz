import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  View,
  Text,
  StyleSheet,
  TouchableOpacity,
  ScrollView,
  FlatList,
  Alert,
  ActivityIndicator,
} from 'react-native';
import { apiService } from '../../services/api';
import { AttemptStartData, Question, AnswerPayload } from '../../types';
import { Theme } from '../../theme';

/**
 * Isolated memoized countdown timer component so the 1-second tick
 * NEVER triggers a full re-render of the question card or option list.
 */
const QuizTimerBadge = React.memo(
  ({
    deadlineMs,
    clockOffsetMs,
    onExpire,
  }: {
    deadlineMs: number;
    clockOffsetMs: number;
    onExpire: () => void;
  }) => {
    const [timeLeft, setTimeLeft] = useState<string>('--:--');
    const expiredRef = useRef(false);

    useEffect(() => {
      const updateTimer = () => {
        const now = Date.now() + clockOffsetMs;
        const diff = deadlineMs - now;

        if (diff <= 0) {
          setTimeLeft('00:00');
          if (!expiredRef.current) {
            expiredRef.current = true;
            onExpire();
          }
          return;
        }

        const minutes = Math.floor(diff / 60000);
        const seconds = Math.floor((diff % 60000) / 1000);
        setTimeLeft(`${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`);
      };

      updateTimer();
      const id = setInterval(updateTimer, 1000);
      return () => clearInterval(id);
    }, [deadlineMs, clockOffsetMs, onExpire]);

    return (
      <View style={styles.timerBadge}>
        <Text style={styles.timerIcon}>⏱</Text>
        <Text style={styles.timerValue}>{timeLeft}</Text>
      </View>
    );
  }
);

const PalettePill = React.memo(
  ({
    index,
    isAnswered,
    isCurrent,
    onPress,
  }: {
    index: number;
    isAnswered: boolean;
    isCurrent: boolean;
    onPress: (idx: number) => void;
  }) => (
    <TouchableOpacity
      style={[
        styles.paletteItem,
        isAnswered && styles.paletteAnswered,
        isCurrent && styles.paletteCurrent,
      ]}
      onPress={() => onPress(index)}
    >
      <Text
        style={[
          styles.paletteText,
          isAnswered && styles.paletteAnsweredText,
          isCurrent && styles.paletteCurrentText,
        ]}
      >
        {index + 1}
      </Text>
    </TouchableOpacity>
  )
);

const PALETTE_ITEM_WIDTH = 42;

export const QuizActiveScreen = ({ route, navigation }: { route: any; navigation: any }) => {
  const { attempt } = route.params as { attempt: AttemptStartData };
  const questions = attempt.questions;

  const [currentIndex, setCurrentIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<number, string | null>>(() => {
    const initial: Record<number, string | null> = {};
    if (attempt.saved_answers) {
      Object.entries(attempt.saved_answers).forEach(([k, v]) => {
        initial[parseInt(k, 10)] = v;
      });
    }
    return initial;
  });

  const [submitting, setSubmitting] = useState(false);
  const [syncStatus, setSyncStatus] = useState<'synced' | 'saving' | 'retrying'>('synced');

  // Pending dirty answers queue + debounce/retry refs for zero answer loss
  const pendingAnswersRef = useRef<Record<number, string | null>>({});
  const debounceTimerRef = useRef<any>(null);
  const retryTimerRef = useRef<any>(null);
  const syncInFlightRef = useRef(false);
  const submittingRef = useRef(false);
  const mountedRef = useRef(true);
  const idempotencyKeyRef = useRef<string>(`qf-${attempt.attempt_id}-${Date.now()}`);

  const deadlineMs = useMemo(() => new Date(attempt.deadline_time).getTime(), [attempt.deadline_time]);
  const clockOffsetMs = useMemo(() => {
    if (!attempt.server_time) return 0;
    const srv = new Date(attempt.server_time).getTime();
    return Number.isFinite(srv) ? srv - Date.now() : 0;
  }, [attempt.server_time]);

  const flushPendingAnswers = useCallback(async () => {
    if (submittingRef.current || syncInFlightRef.current) return;
    const entries = Object.entries(pendingAnswersRef.current);
    if (entries.length === 0) {
      if (mountedRef.current) setSyncStatus('synced');
      return;
    }

    syncInFlightRef.current = true;
    if (mountedRef.current) setSyncStatus('saving');

    const snapshot: Record<number, string | null> = {};
    const payload: AnswerPayload[] = entries.map(([k, v]) => {
      const qid = parseInt(k, 10);
      snapshot[qid] = v;
      return { question_id: qid, selected_option: v };
    });

    const res = await apiService.saveAnswersBatch(attempt.attempt_id, payload);
    syncInFlightRef.current = false;

    if (!mountedRef.current) return;

    if (res.success) {
      for (const [qidStr, opt] of Object.entries(snapshot)) {
        const qid = parseInt(qidStr, 10);
        if (pendingAnswersRef.current[qid] === opt) {
          delete pendingAnswersRef.current[qid];
        }
      }
      if (Object.keys(pendingAnswersRef.current).length > 0) {
        flushPendingAnswers();
      } else {
        setSyncStatus('synced');
      }
    } else {
      setSyncStatus('retrying');
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      retryTimerRef.current = setTimeout(() => {
        flushPendingAnswers();
      }, 2500);
    }
  }, [attempt.attempt_id]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, []);

  const handleSelectOption = useCallback(
    (letter: string) => {
      const q = questions[currentIndex];
      if (!q) return;

      setAnswers((prev) => ({ ...prev, [q.id]: letter }));
      pendingAnswersRef.current[q.id] = letter;
      setSyncStatus('saving');

      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = setTimeout(() => {
        flushPendingAnswers();
      }, 350);
    },
    [currentIndex, questions, flushPendingAnswers]
  );

  const handleAutoSubmit = useCallback(async () => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);

    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);

    // Atomically flush any unsaved answers inside finishQuiz so zero answers are lost!
    const pendingList: AnswerPayload[] = Object.entries(pendingAnswersRef.current).map(([k, v]) => ({
      question_id: parseInt(k, 10),
      selected_option: v,
    }));

    const res = await apiService.finishQuiz(
      attempt.attempt_id,
      idempotencyKeyRef.current,
      pendingList
    );

    if (res.success && res.data) {
      pendingAnswersRef.current = {};
      navigation.replace('Result', {
        attemptId: attempt.attempt_id,
        initialResult: res.data,
      });
    } else {
      // Fallback: check if server already finalized the attempt
      const fallback = await apiService.getResult(attempt.attempt_id);
      if (fallback.success && fallback.data && fallback.data.status !== 'in_progress') {
        navigation.replace('Result', {
          attemptId: attempt.attempt_id,
          initialResult: fallback.data,
        });
      } else {
        submittingRef.current = false;
        setSubmitting(false);
        Alert.alert(
          'Submission Error',
          res.error || 'Could not submit your exam right now. Your answers are preserved — please tap Submit again.'
        );
      }
    }
  }, [attempt.attempt_id, navigation]);

  const confirmSubmit = useCallback(() => {
    const answeredCount = Object.values(answers).filter(Boolean).length;
    const unanswered = questions.length - answeredCount;

    let msg = 'Are you ready to submit your assessment?';
    if (unanswered > 0) {
      msg = `You have ${unanswered} unanswered question(s). Are you sure you want to finish and submit now?`;
    }

    Alert.alert('Submit Assessment', msg, [
      { text: 'Review Questions', style: 'cancel' },
      { text: 'Submit Exam', style: 'destructive', onPress: handleAutoSubmit },
    ]);
  }, [answers, questions.length, handleAutoSubmit]);

  const handleSelectQuestionIndex = useCallback((idx: number) => {
    setCurrentIndex(idx);
  }, []);

  const renderPaletteItem = useCallback(
    ({ item, index }: { item: Question; index: number }) => (
      <PalettePill
        index={index}
        isAnswered={Boolean(answers[item.id])}
        isCurrent={index === currentIndex}
        onPress={handleSelectQuestionIndex}
      />
    ),
    [answers, currentIndex, handleSelectQuestionIndex]
  );

  const paletteKeyExtractor = useCallback((item: Question) => item.id.toString(), []);
  const getPaletteItemLayout = useCallback(
    (_: any, index: number) => ({
      length: PALETTE_ITEM_WIDTH,
      offset: PALETTE_ITEM_WIDTH * index,
      index,
    }),
    []
  );

  const currentQ = questions[currentIndex];
  const selectedOpt = currentQ ? answers[currentQ.id] || null : null;

  const options = useMemo(
    () =>
      currentQ
        ? [
            { opt: 'A', text: currentQ.option_a },
            { opt: 'B', text: currentQ.option_b },
            { opt: 'C', text: currentQ.option_c },
            { opt: 'D', text: currentQ.option_d },
          ]
        : [],
    [currentQ]
  );

  return (
    <View style={styles.container}>
      {/* Top Header Bar */}
      <View style={styles.topBar}>
        <View style={styles.topBarLeft}>
          <View style={styles.badgeLine}>
            <View style={styles.typeBadge}>
              <Text style={styles.typeBadgeText}>ASSESSMENT EXAM</Text>
            </View>
            <Text style={styles.syncText}>
              {syncStatus === 'saving'
                ? 'Saving...'
                : syncStatus === 'retrying'
                ? 'Offline — retrying'
                : '✓ Saved'}
            </Text>
          </View>
          <Text style={styles.quizName} numberOfLines={1}>
            {attempt.quiz_title}
          </Text>
        </View>

        <QuizTimerBadge
          deadlineMs={deadlineMs}
          clockOffsetMs={clockOffsetMs}
          onExpire={handleAutoSubmit}
        />
      </View>

      {/* Virtualized Horizontal Question Navigator Palette */}
      <View style={styles.paletteContainer}>
        <FlatList
          horizontal
          data={questions}
          keyExtractor={paletteKeyExtractor}
          renderItem={renderPaletteItem}
          getItemLayout={getPaletteItemLayout}
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.paletteScroll}
          initialNumToRender={15}
          maxToRenderPerBatch={15}
          windowSize={5}
        />
      </View>

      {/* Question Card */}
      <ScrollView style={styles.scrollArea} contentContainerStyle={styles.cardContent}>
        {currentQ ? (
          <View style={styles.questionCard}>
            <View style={styles.qHeader}>
              <View style={styles.qBadge}>
                <Text style={styles.qBadgeText}>
                  Question {currentIndex + 1} of {questions.length}
                </Text>
              </View>
              <View style={styles.marksBadge}>
                <Text style={styles.marksText}>{currentQ.marks} Mark(s)</Text>
              </View>
            </View>

            <Text style={styles.qText}>{currentQ.question_text}</Text>

            <View style={styles.optionsList}>
              {options.map((item) => {
                const isSelected = selectedOpt === item.opt;
                return (
                  <TouchableOpacity
                    key={item.opt}
                    activeOpacity={0.7}
                    style={[styles.optionCard, isSelected && styles.optionCardSelected]}
                    onPress={() => handleSelectOption(item.opt)}
                  >
                    <View style={[styles.letterCircle, isSelected && styles.letterCircleSelected]}>
                      <Text style={[styles.letterText, isSelected && styles.letterTextSelected]}>
                        {item.opt}
                      </Text>
                    </View>
                    <Text style={[styles.optionText, isSelected && styles.optionTextSelected]}>
                      {item.text}
                    </Text>
                  </TouchableOpacity>
                );
              })}
            </View>
          </View>
        ) : null}
      </ScrollView>

      {/* Bottom Action Bar */}
      <View style={styles.bottomBar}>
        <TouchableOpacity
          style={[styles.navBtn, currentIndex === 0 && styles.navBtnDisabled]}
          disabled={currentIndex === 0}
          onPress={() => setCurrentIndex((prev) => Math.max(0, prev - 1))}
        >
          <Text style={styles.navBtnText}>◀ Back</Text>
        </TouchableOpacity>

        <View style={styles.pageIndicator}>
          <Text style={styles.pageIndicatorText}>
            {currentIndex + 1} / {questions.length}
          </Text>
        </View>

        {currentIndex < questions.length - 1 ? (
          <TouchableOpacity
            style={[styles.navBtn, styles.navBtnPrimary]}
            onPress={() => setCurrentIndex((prev) => Math.min(questions.length - 1, prev + 1))}
          >
            <Text style={styles.navBtnPrimaryText}>Next ▶</Text>
          </TouchableOpacity>
        ) : (
          <TouchableOpacity
            style={[styles.navBtn, styles.submitBtn]}
            onPress={confirmSubmit}
            disabled={submitting}
          >
            {submitting ? (
              <ActivityIndicator color="#FFFFFF" size="small" />
            ) : (
              <Text style={styles.submitBtnText}>Submit Exam ✓</Text>
            )}
          </TouchableOpacity>
        )}
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: Theme.BG_MAIN,
  },
  topBar: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 12,
    backgroundColor: Theme.BG_CARD,
    borderBottomWidth: 1,
    borderBottomColor: Theme.BORDER,
  },
  topBarLeft: {
    flex: 1,
    marginRight: 10,
  },
  badgeLine: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    marginBottom: 4,
  },
  typeBadge: {
    alignSelf: 'flex-start',
    backgroundColor: Theme.PRIMARY_LIGHT,
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 4,
  },
  typeBadgeText: {
    color: Theme.PRIMARY,
    fontSize: 10,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  syncText: {
    fontSize: 11,
    color: Theme.TEXT_MUTED,
    fontWeight: '600',
  },
  quizName: {
    fontSize: 15,
    fontWeight: 'bold',
    color: Theme.TEXT_MAIN,
  },
  timerBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: Theme.PURPLE_LIGHT,
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: Theme.PURPLE,
  },
  timerIcon: {
    fontSize: 14,
    marginRight: 4,
  },
  timerValue: {
    color: Theme.PURPLE,
    fontSize: 14,
    fontWeight: '700',
  },
  paletteContainer: {
    backgroundColor: Theme.BG_CARD,
    borderBottomWidth: 1,
    borderBottomColor: Theme.BORDER,
    paddingVertical: 8,
  },
  paletteScroll: {
    paddingHorizontal: 14,
  },
  paletteItem: {
    width: 34,
    height: 34,
    marginRight: 8,
    borderRadius: 8,
    backgroundColor: Theme.BG_MAIN,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    justifyContent: 'center',
    alignItems: 'center',
  },
  paletteAnswered: {
    backgroundColor: Theme.SUCCESS_LIGHT,
    borderColor: Theme.SUCCESS,
  },
  paletteCurrent: {
    borderColor: Theme.PRIMARY,
    borderWidth: 2,
    backgroundColor: Theme.PRIMARY_LIGHT,
  },
  paletteText: {
    color: Theme.TEXT_MUTED,
    fontSize: 13,
    fontWeight: 'bold',
  },
  paletteAnsweredText: {
    color: Theme.SUCCESS,
  },
  paletteCurrentText: {
    color: Theme.PRIMARY,
  },
  scrollArea: {
    flex: 1,
  },
  cardContent: {
    padding: 16,
  },
  questionCard: {
    backgroundColor: Theme.BG_CARD,
    borderRadius: 14,
    padding: 18,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.04,
    shadowRadius: 6,
    elevation: 2,
  },
  qHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 12,
  },
  qBadge: {
    backgroundColor: Theme.PRIMARY_LIGHT,
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 6,
  },
  qBadgeText: {
    fontSize: 12,
    fontWeight: 'bold',
    color: Theme.PRIMARY,
  },
  marksBadge: {
    backgroundColor: Theme.BG_MAIN,
    borderRadius: 6,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderWidth: 1,
    borderColor: Theme.BORDER,
  },
  marksText: {
    fontSize: 12,
    color: Theme.TEXT_MUTED,
    fontWeight: '600',
  },
  qText: {
    fontSize: 16,
    fontWeight: '600',
    color: Theme.TEXT_MAIN,
    lineHeight: 23,
    marginBottom: 18,
  },
  optionsList: {
    gap: 10,
  },
  optionCard: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: Theme.BG_CARD,
    borderRadius: 10,
    padding: 12,
    borderWidth: 1,
    borderColor: Theme.BORDER,
  },
  optionCardSelected: {
    borderColor: Theme.PURPLE,
    backgroundColor: Theme.PURPLE_LIGHT,
    borderWidth: 1.5,
  },
  letterCircle: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: Theme.BG_MAIN,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    justifyContent: 'center',
    alignItems: 'center',
    marginRight: 12,
  },
  letterCircleSelected: {
    backgroundColor: Theme.PURPLE,
    borderColor: Theme.PURPLE,
  },
  letterText: {
    fontSize: 13,
    fontWeight: 'bold',
    color: Theme.TEXT_MUTED,
  },
  letterTextSelected: {
    color: '#FFFFFF',
  },
  optionText: {
    fontSize: 14,
    color: Theme.TEXT_MAIN,
    flex: 1,
    lineHeight: 20,
  },
  optionTextSelected: {
    color: Theme.PURPLE,
    fontWeight: '600',
  },
  bottomBar: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: 14,
    backgroundColor: Theme.BG_CARD,
    borderTopWidth: 1,
    borderTopColor: Theme.BORDER,
  },
  pageIndicator: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 6,
    backgroundColor: Theme.BG_MAIN,
  },
  pageIndicatorText: {
    fontSize: 13,
    fontWeight: 'bold',
    color: Theme.TEXT_MUTED,
  },
  navBtn: {
    paddingVertical: 10,
    paddingHorizontal: 18,
    borderRadius: 8,
    backgroundColor: Theme.BG_MAIN,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    minWidth: 84,
    alignItems: 'center',
  },
  navBtnDisabled: {
    opacity: 0.35,
  },
  navBtnText: {
    color: Theme.TEXT_MAIN,
    fontSize: 13,
    fontWeight: '600',
  },
  navBtnPrimary: {
    backgroundColor: Theme.PRIMARY,
    borderColor: Theme.PRIMARY,
  },
  navBtnPrimaryText: {
    color: '#FFFFFF',
    fontSize: 13,
    fontWeight: 'bold',
  },
  submitBtn: {
    backgroundColor: Theme.SUCCESS,
    borderColor: Theme.SUCCESS,
  },
  submitBtnText: {
    color: '#FFFFFF',
    fontSize: 13,
    fontWeight: 'bold',
  },
});
