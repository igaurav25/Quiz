import React, { useEffect, useState, useCallback, useMemo } from 'react';
import {
  View,
  Text,
  FlatList,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
  RefreshControl,
  Modal,
  TextInput,
} from 'react-native';
import { apiService } from '../../services/api';
import { Quiz, AttemptSummary } from '../../types';
import { Theme } from '../../theme';

const PAGE_SIZE = 20;

const QuizCardItem = React.memo(
  ({ item, onStart }: { item: Quiz; onStart: (quiz: Quiz) => void }) => (
    <View style={styles.card}>
      <View style={styles.cardHeader}>
        <View style={styles.cardIconBox}>
          <Text style={styles.cardIcon}>✦</Text>
        </View>
        <View style={styles.cardTitleBox}>
          <Text style={styles.quizTitle}>{item.title}</Text>
          <Text style={styles.quizMeta}>
            ⏱ {item.duration_minutes} Mins  •  ❓ {item.question_count} Questions
          </Text>
        </View>
      </View>

      <Text style={styles.quizDesc} numberOfLines={2}>
        {item.description || 'Comprehensive evaluation covering architecture and system concepts.'}
      </Text>

      <View style={styles.badgeRow}>
        {item.join_code ? (
          <View style={[styles.badge, styles.badgeCode]}>
            <Text style={styles.badgeCodeText}>🔑 {item.join_code}</Text>
          </View>
        ) : null}
        <View style={styles.badge}>
          <Text style={styles.badgeText}>🎯 {item.total_marks} Marks</Text>
        </View>
        <View style={[styles.badge, styles.badgePass]}>
          <Text style={styles.badgeTextPass}>🏆 {item.passing_percentage}% Passing</Text>
        </View>
      </View>

      <TouchableOpacity
        style={styles.startButton}
        activeOpacity={0.8}
        onPress={() => onStart(item)}
      >
        <Text style={styles.startButtonText}>Start Assessment →</Text>
      </TouchableOpacity>
    </View>
  )
);

export const DashboardScreen = ({ navigation }: { navigation: any }) => {
  const [quizzes, setQuizzes] = useState<Quiz[]>([]);
  const [history, setHistory] = useState<AttemptSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [joinModalVisible, setJoinModalVisible] = useState(false);
  const [joinCodeInput, setJoinCodeInput] = useState('');
  const [joinLoading, setJoinLoading] = useState(false);
  const [joinError, setJoinError] = useState('');

  const handleJoinByCode = useCallback(async () => {
    if (!joinCodeInput.trim()) {
      setJoinError('Please enter a join code.');
      return;
    }
    setJoinLoading(true);
    setJoinError('');
    const res = await apiService.joinQuizByCode(joinCodeInput.trim());
    setJoinLoading(false);
    if (res.success && res.data) {
      setJoinModalVisible(false);
      setJoinCodeInput('');
      navigation.navigate('QuizDetail', { quiz: res.data });
    } else {
      setJoinError(res.error || 'Invalid join code or quiz not available.');
    }
  }, [joinCodeInput, navigation]);

  const loadData = useCallback(async () => {
    const [qRes, hRes] = await Promise.all([
      apiService.getPublishedQuizzes(PAGE_SIZE, 0),
      apiService.getMyHistory(20, 0),
    ]);
    if (qRes.success && qRes.data) {
      setQuizzes(qRes.data);
      setHasMore(Boolean(qRes.hasMore));
    }
    if (hRes.success && hRes.data) {
      setHistory(hRes.data);
    }
    setLoading(false);
    setRefreshing(false);
  }, []);

  const loadMoreQuizzes = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    const res = await apiService.getPublishedQuizzes(PAGE_SIZE, quizzes.length);
    if (res.success && res.data) {
      setQuizzes((prev) => {
        const seen = new Set(prev.map((q) => q.id));
        const fresh = res.data!.filter((q) => !seen.has(q.id));
        return [...prev, ...fresh];
      });
      setHasMore(Boolean(res.hasMore));
    }
    setLoadingMore(false);
  }, [loadingMore, hasMore, quizzes.length]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const onRefresh = useCallback(() => {
    setRefreshing(true);
    loadData();
  }, [loadData]);

  const handleNavigateQuiz = useCallback(
    (quiz: Quiz) => {
      navigation.navigate('QuizDetail', { quiz });
    },
    [navigation]
  );

  const avgScore = useMemo(
    () =>
      history.length
        ? Math.round(history.reduce((acc, h) => acc + h.percentage, 0) / history.length)
        : 0,
    [history]
  );

  const passRate = useMemo(
    () =>
      history.length
        ? `${Math.round((history.filter((h) => h.is_passed).length / history.length) * 100)}%`
        : '100%',
    [history]
  );

  const openJoinModal = useCallback(() => {
    setJoinCodeInput('');
    setJoinError('');
    setJoinModalVisible(true);
  }, []);

  const renderHeader = useCallback(
    () => (
      <View style={styles.headerSection}>
        <View style={styles.heroCard}>
          <View style={styles.heroTop}>
            <View>
              <Text style={styles.heroBadge}>COGNIFY PERFORMANCE</Text>
              <Text style={styles.heroTitle}>Exams Average</Text>
            </View>
            <View style={styles.heroStatBadge}>
              <Text style={styles.heroStatText}>{avgScore}%</Text>
            </View>
          </View>

          <Text style={styles.heroSubtitle}>
            {history.length} completed assessments • Real-time knowledge scoring
          </Text>

          <View style={styles.heroStatsRow}>
            <View style={styles.heroMiniStat}>
              <Text style={styles.miniStatLabel}>Completed</Text>
              <Text style={styles.miniStatValue}>{history.length}</Text>
            </View>
            <View style={styles.heroStatDivider} />
            <View style={styles.heroMiniStat}>
              <Text style={styles.miniStatLabel}>Available</Text>
              <Text style={styles.miniStatValue}>{quizzes.length}</Text>
            </View>
            <View style={styles.heroStatDivider} />
            <View style={styles.heroMiniStat}>
              <Text style={styles.miniStatLabel}>Pass Rate</Text>
              <Text style={styles.miniStatValue}>{passRate}</Text>
            </View>
          </View>
        </View>

        <View style={styles.sectionHeader}>
          <View>
            <Text style={styles.sectionTitle}>Available Assessments</Text>
            <Text style={styles.sectionCount}>{quizzes.length} tests</Text>
          </View>
          <TouchableOpacity
            style={styles.joinCodeBtn}
            activeOpacity={0.8}
            onPress={openJoinModal}
          >
            <Text style={styles.joinCodeBtnText}>🔑 Enter Join Code</Text>
          </TouchableOpacity>
        </View>
      </View>
    ),
    [avgScore, history.length, quizzes.length, passRate, openJoinModal]
  );

  const renderQuizItem = useCallback(
    ({ item }: { item: Quiz }) => <QuizCardItem item={item} onStart={handleNavigateQuiz} />,
    [handleNavigateQuiz]
  );

  const keyExtractor = useCallback((item: Quiz) => item.id.toString(), []);

  return (
    <View style={styles.container}>
      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color={Theme.PRIMARY} />
          <Text style={styles.loadingText}>Loading QuizFlow assessments...</Text>
        </View>
      ) : (
        <FlatList
          data={quizzes}
          keyExtractor={keyExtractor}
          ListHeaderComponent={renderHeader}
          renderItem={renderQuizItem}
          contentContainerStyle={styles.listContent}
          showsVerticalScrollIndicator={false}
          initialNumToRender={8}
          maxToRenderPerBatch={8}
          windowSize={7}
          removeClippedSubviews
          onEndReached={loadMoreQuizzes}
          onEndReachedThreshold={0.4}
          ListFooterComponent={
            loadingMore ? <ActivityIndicator style={{ marginVertical: 12 }} color={Theme.PRIMARY} /> : null
          }
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={Theme.PRIMARY} />
          }
          ListEmptyComponent={
            <View style={styles.emptyBox}>
              <Text style={styles.emptyIcon}>📝</Text>
              <Text style={styles.emptyTitle}>No Assessments Available</Text>
              <Text style={styles.emptyText}>Published quizzes will appear here automatically.</Text>
            </View>
          }
        />
      )}

      {/* Join Quiz by Code Modal */}
      <Modal
        visible={joinModalVisible}
        transparent
        animationType="fade"
        onRequestClose={() => setJoinModalVisible(false)}
      >
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>Join Private Quiz</Text>
            <Text style={styles.modalSubtitle}>
              Enter the unique 7-character quiz code provided by your teacher (e.g. QF-A1B2)
            </Text>

            <TextInput
              style={styles.modalInput}
              placeholder="QF-XXXX"
              placeholderTextColor={Theme.TEXT_DIM}
              value={joinCodeInput}
              onChangeText={(t) => setJoinCodeInput(t.toUpperCase())}
              autoCapitalize="characters"
              maxLength={10}
            />

            {joinError ? <Text style={styles.modalError}>{joinError}</Text> : null}

            <View style={styles.modalBtnRow}>
              <TouchableOpacity
                style={styles.modalCancelBtn}
                onPress={() => setJoinModalVisible(false)}
              >
                <Text style={styles.modalCancelText}>Cancel</Text>
              </TouchableOpacity>

              <TouchableOpacity
                style={styles.modalSubmitBtn}
                disabled={joinLoading}
                onPress={handleJoinByCode}
              >
                {joinLoading ? (
                  <ActivityIndicator color="#FFFFFF" size="small" />
                ) : (
                  <Text style={styles.modalSubmitText}>Join Quiz</Text>
                )}
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
};

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: Theme.BG_MAIN },
  listContent: { paddingHorizontal: 16, paddingTop: 16, paddingBottom: 28 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 30 },
  loadingText: { marginTop: 12, fontSize: 14, color: Theme.TEXT_MUTED },
  headerSection: { marginBottom: 16 },
  heroCard: {
    backgroundColor: Theme.PRIMARY,
    borderRadius: 16,
    padding: 18,
    shadowColor: '#4F46E5',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.15,
    shadowRadius: 10,
    elevation: 4,
  },
  heroTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start' },
  heroBadge: { color: '#C7D2FE', fontSize: 10, fontWeight: '700', letterSpacing: 0.8, marginBottom: 2 },
  heroTitle: { fontSize: 20, fontWeight: 'bold', color: '#FFFFFF' },
  heroStatBadge: { backgroundColor: 'rgba(255, 255, 255, 0.2)', paddingHorizontal: 12, paddingVertical: 4, borderRadius: 12 },
  heroStatText: { color: '#FFFFFF', fontSize: 18, fontWeight: 'bold' },
  heroSubtitle: { fontSize: 12, color: '#E0E7FF', marginTop: 6, marginBottom: 14 },
  heroStatsRow: {
    flexDirection: 'row',
    backgroundColor: 'rgba(0, 0, 0, 0.12)',
    borderRadius: 10,
    paddingVertical: 10,
    paddingHorizontal: 12,
    justifyContent: 'space-around',
    alignItems: 'center',
  },
  heroMiniStat: { alignItems: 'center', flex: 1 },
  heroStatDivider: { width: 1, height: 24, backgroundColor: 'rgba(255, 255, 255, 0.2)' },
  miniStatLabel: { color: '#C7D2FE', fontSize: 11, fontWeight: '500' },
  miniStatValue: { color: '#FFFFFF', fontSize: 15, fontWeight: 'bold', marginTop: 2 },
  sectionHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 22, marginBottom: 10, paddingHorizontal: 2 },
  sectionTitle: { fontSize: 17, fontWeight: '700', color: Theme.TEXT_MAIN },
  sectionCount: { fontSize: 12, color: Theme.TEXT_MUTED, fontWeight: '500' },
  card: {
    backgroundColor: Theme.BG_CARD,
    borderRadius: 14,
    padding: 16,
    marginBottom: 14,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.04,
    shadowRadius: 6,
    elevation: 2,
  },
  cardHeader: { flexDirection: 'row', alignItems: 'center', marginBottom: 8 },
  cardIconBox: { width: 36, height: 36, borderRadius: 8, backgroundColor: Theme.PURPLE_LIGHT, justifyContent: 'center', alignItems: 'center', marginRight: 10 },
  cardIcon: { fontSize: 16, color: Theme.PURPLE, fontWeight: 'bold' },
  cardTitleBox: { flex: 1 },
  quizTitle: { fontSize: 16, fontWeight: 'bold', color: Theme.TEXT_MAIN },
  quizMeta: { fontSize: 12, color: Theme.TEXT_MUTED, marginTop: 2 },
  quizDesc: { fontSize: 13, color: Theme.TEXT_MUTED, lineHeight: 18, marginTop: 2, marginBottom: 12 },
  badgeRow: { flexDirection: 'row', gap: 8, marginBottom: 14, flexWrap: 'wrap' },
  badge: { backgroundColor: Theme.BG_MAIN, borderRadius: 6, paddingHorizontal: 8, paddingVertical: 4, borderWidth: 1, borderColor: Theme.BORDER },
  badgeText: { color: Theme.TEXT_MUTED, fontSize: 11, fontWeight: '600' },
  badgePass: { backgroundColor: Theme.SUCCESS_LIGHT, borderColor: Theme.SUCCESS },
  badgeTextPass: { color: Theme.SUCCESS, fontSize: 11, fontWeight: '600' },
  startButton: { backgroundColor: Theme.PRIMARY, borderRadius: 8, paddingVertical: 11, alignItems: 'center' },
  startButtonText: { color: '#FFFFFF', fontSize: 14, fontWeight: '700' },
  emptyBox: { alignItems: 'center', paddingVertical: 40 },
  emptyIcon: { fontSize: 36, marginBottom: 10 },
  emptyTitle: { fontSize: 16, fontWeight: '700', color: Theme.TEXT_MAIN, marginBottom: 4 },
  emptyText: { fontSize: 13, color: Theme.TEXT_MUTED },
  joinCodeBtn: { backgroundColor: Theme.PRIMARY_LIGHT, borderRadius: 8, paddingHorizontal: 12, paddingVertical: 7, borderWidth: 1, borderColor: Theme.PRIMARY },
  joinCodeBtnText: { color: Theme.PRIMARY, fontSize: 12, fontWeight: '700' },
  badgeCode: { backgroundColor: Theme.PRIMARY_LIGHT, borderColor: Theme.PRIMARY },
  badgeCodeText: { color: Theme.PRIMARY, fontSize: 11, fontWeight: '700' },
  modalOverlay: { flex: 1, backgroundColor: 'rgba(15, 23, 42, 0.65)', justifyContent: 'center', alignItems: 'center', padding: 24 },
  modalCard: { width: '100%', backgroundColor: Theme.BG_CARD, borderRadius: 16, padding: 22, borderWidth: 1, borderColor: Theme.BORDER },
  modalTitle: { fontSize: 18, fontWeight: 'bold', color: Theme.TEXT_MAIN, marginBottom: 6 },
  modalSubtitle: { fontSize: 13, color: Theme.TEXT_MUTED, marginBottom: 16, lineHeight: 18 },
  modalInput: {
    backgroundColor: Theme.BG_INPUT,
    borderWidth: 1,
    borderColor: Theme.BORDER,
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 18,
    fontWeight: 'bold',
    color: Theme.TEXT_MAIN,
    textAlign: 'center',
    letterSpacing: 2,
    marginBottom: 10,
  },
  modalError: { color: Theme.DANGER, fontSize: 12, marginBottom: 10, textAlign: 'center' },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 6 },
  modalCancelBtn: { flex: 1, paddingVertical: 12, borderRadius: 8, borderWidth: 1, borderColor: '#CBD5E1', backgroundColor: '#F1F5F9', alignItems: 'center' },
  modalCancelText: { fontSize: 14, fontWeight: '700', color: '#334155' },
  modalSubmitBtn: { flex: 1, paddingVertical: 12, borderRadius: 8, backgroundColor: Theme.PRIMARY, alignItems: 'center' },
  modalSubmitText: { fontSize: 14, fontWeight: 'bold', color: '#FFFFFF' },
});
