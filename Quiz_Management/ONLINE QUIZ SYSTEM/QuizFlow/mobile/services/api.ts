import { networkConfig } from './config';
import {
  User,
  Quiz,
  AttemptStartData,
  AttemptResult,
  AttemptSummary,
  AnswerPayload,
  BatchAnswerResponse,
} from '../types';

export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  hasMore?: boolean;
  totalCount?: number;
}

const DEFAULT_TIMEOUT_MS = 12000;
const RETRYABLE_STATUSES = new Set([408, 429, 502, 503, 504]);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class ApiService {
  private token: string | null = null;
  private currentUser: User | null = null;
  private inflightGets = new Map<string, Promise<ApiResponse<any>>>();

  setToken(t: string | null) {
    this.token = t;
  }

  getToken(): string | null {
    return this.token;
  }

  setUser(u: User | null) {
    this.currentUser = u;
  }

  getUser(): User | null {
    return this.currentUser;
  }

  private async request<T>(
    path: string,
    method: string = 'GET',
    body?: any,
    options?: {
      timeoutMs?: number;
      retries?: number;
      extraHeaders?: Record<string, string>;
      dedupe?: boolean;
    }
  ): Promise<ApiResponse<T>> {
    const url = `${networkConfig.apiUrl}${path}`;
    const upperMethod = method.toUpperCase();
    const shouldDedupe = upperMethod === 'GET' && (options?.dedupe ?? true);

    if (shouldDedupe && this.inflightGets.has(url)) {
      return this.inflightGets.get(url) as Promise<ApiResponse<T>>;
    }

    const execute = async (): Promise<ApiResponse<T>> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(options?.extraHeaders || {}),
      };
      if (this.token) {
        headers['Authorization'] = `Bearer ${this.token}`;
      }

      const maxRetries = options?.retries ?? (upperMethod === 'GET' ? 2 : 1);
      const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS;

      for (let attempt = 0; attempt <= maxRetries; attempt++) {
        const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
        const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;

        try {
          const response = await fetch(url, {
            method: upperMethod,
            headers,
            body: body !== undefined ? JSON.stringify(body) : undefined,
            signal: controller ? controller.signal : undefined,
          });

          if (timer) clearTimeout(timer);

          if (!response.ok && RETRYABLE_STATUSES.has(response.status) && attempt < maxRetries) {
            await sleep(250 * Math.pow(2, attempt));
            continue;
          }

          const json = await response.json().catch(() => null);
          const totalHeader = response.headers.get('X-Total-Count');
          const hasMoreHeader = response.headers.get('X-Has-More');

          if (response.ok) {
            return {
              success: true,
              data: json,
              totalCount: totalHeader ? parseInt(totalHeader, 10) : undefined,
              hasMore: hasMoreHeader ? hasMoreHeader === 'true' : undefined,
            };
          }

          const errorMsg = json?.detail || `Error (${response.status}): ${response.statusText}`;
          return {
            success: false,
            error: typeof errorMsg === 'string' ? errorMsg : JSON.stringify(errorMsg),
          };
        } catch (err: any) {
          if (timer) clearTimeout(timer);
          if (attempt < maxRetries) {
            await sleep(250 * Math.pow(2, attempt));
            continue;
          }
          const isAbort = err?.name === 'AbortError';
          return {
            success: false,
            error: isAbort
              ? 'Request timed out. Please check your network connection and try again.'
              : `Network request failed. Ensure backend is running at ${networkConfig.apiUrl}. Details: ${err?.message || 'Unknown error'}`,
          };
        }
      }

      return { success: false, error: 'Request failed after retries.' };
    };

    const promise = execute();
    if (shouldDedupe) {
      this.inflightGets.set(url, promise);
      promise.finally(() => {
        this.inflightGets.delete(url);
      });
    }
    return promise;
  }

  async checkHealth() {
    return this.request<{ status: string; database: string }>('/health', 'GET', undefined, {
      timeoutMs: 5000,
      retries: 0,
    });
  }

  async login(email: string, password: string) {
    const res = await this.request<{ access_token: string; user: User }>(
      '/auth/login',
      'POST',
      { email, password },
      { retries: 0 }
    );
    if (res.success && res.data) {
      this.setToken(res.data.access_token);
      this.setUser(res.data.user);
    }
    return res;
  }

  async register(
    email: string,
    full_name: string,
    password: string,
    confirm_password: string,
    role: string = 'participant'
  ) {
    return this.request<User>(
      '/auth/register',
      'POST',
      {
        email,
        full_name,
        password,
        confirm_password,
        role,
      },
      { retries: 0 }
    );
  }

  logout() {
    this.token = null;
    this.currentUser = null;
    this.inflightGets.clear();
  }

  async getPublishedQuizzes(limit: number = 30, offset: number = 0) {
    return this.request<Quiz[]>(`/quizzes?limit=${limit}&offset=${offset}`);
  }

  async getQuizByCode(joinCode: string) {
    return this.request<Quiz>(`/quizzes/by-code/${joinCode.trim().toUpperCase()}`);
  }

  async joinQuizByCode(joinCode: string) {
    return this.request<Quiz>('/quizzes/join-by-code', 'POST', {
      join_code: joinCode.trim().toUpperCase(),
    });
  }

  async getQuizDetails(quizId: number) {
    return this.request<Quiz>(`/quizzes/${quizId}`);
  }

  async startQuiz(quizId: number) {
    return this.request<AttemptStartData>(`/attempts/quizzes/${quizId}/start`, 'POST', undefined, {
      retries: 1,
    });
  }

  async saveAnswer(attemptId: number, questionId: number, selectedOption: string | null) {
    return this.request<{ saved: boolean }>(
      `/attempts/${attemptId}/answers`,
      'POST',
      {
        question_id: questionId,
        selected_option: selectedOption,
      },
      { retries: 2 }
    );
  }

  async saveAnswersBatch(attemptId: number, answers: AnswerPayload[]) {
    return this.request<BatchAnswerResponse>(
      `/attempts/${attemptId}/answers/batch`,
      'POST',
      { answers },
      { retries: 2 }
    );
  }

  async finishQuiz(
    attemptId: number,
    idempotencyKey?: string,
    pendingAnswers?: AnswerPayload[]
  ) {
    const extraHeaders: Record<string, string> = {};
    if (idempotencyKey) {
      extraHeaders['Idempotency-Key'] = idempotencyKey;
    }
    const body = pendingAnswers && pendingAnswers.length > 0 ? { answers: pendingAnswers } : undefined;
    return this.request<AttemptResult>(`/attempts/${attemptId}/finish`, 'POST', body, {
      timeoutMs: 20000,
      retries: 2,
      extraHeaders,
    });
  }

  async getResult(attemptId: number) {
    return this.request<AttemptResult>(`/attempts/${attemptId}/result`);
  }

  async getMyHistory(limit: number = 30, offset: number = 0) {
    return this.request<AttemptSummary[]>(`/attempts/my-history?limit=${limit}&offset=${offset}`);
  }
}

export const apiService = new ApiService();
