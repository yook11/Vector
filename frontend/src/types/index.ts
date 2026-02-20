/** Sentiment categories for news analysis. */
export type Sentiment = "positive" | "negative" | "neutral";

/** Minimal keyword info embedded in NewsResponse. */
export interface KeywordBrief {
  id: number;
  keyword: string;
  category: string;
}

/** AI analysis result embedded in NewsResponse. */
export interface AnalysisResponse {
  titleJa: string;
  summaryJa: string;
  sentiment: Sentiment;
  impactScore: number;
  keyTopics: string[] | null;
  reasoning: string | null;
  aiProvider: string;
  analyzedAt: string;
}

/** Single news article with analysis and keywords. */
export interface NewsResponse {
  id: number;
  titleOriginal: string;
  url: string;
  source: string;
  publishedAt: string | null;
  fetchedAt: string;
  keywords: KeywordBrief[];
  analysis: AnalysisResponse | null;
  isWatched: boolean;
}

/** Paginated list of news articles. */
export interface PaginatedNewsResponse {
  items: NewsResponse[];
  total: number;
  page: number;
  perPage: number;
  totalPages: number;
}

/** Full keyword info for keyword list and settings. */
export interface KeywordResponse {
  id: number;
  keyword: string;
  category: string;
  isActive: boolean;
  articleCount: number;
  createdAt: string;
}

/** GET /keywords response wrapper. */
export interface KeywordListResponse {
  items: KeywordResponse[];
}

/** POST /keywords request body. */
export interface KeywordCreate {
  keyword: string;
  category?: string;
}

/** PATCH /keywords/{id} request body. */
export interface KeywordUpdate {
  isActive?: boolean | null;
}

/** POST /news/fetch request body. */
export interface NewsFetchRequest {
  keywordIds?: number[] | null;
}

/** POST /news/fetch response. */
export interface NewsFetchResponse {
  message: string;
  keywordsCount: number;
  jobId: string;
}

/** Query parameters for GET /news. */
export interface NewsQuery {
  keywordId?: number;
  myKeywords?: boolean;
  sentiment?: Sentiment;
  minImpact?: number;
  sortBy?: "publishedAt" | "impactScore";
  sortOrder?: "asc" | "desc";
  page?: number;
  perPage?: number;
}

/** POST /auth/login request body. */
export interface LoginRequest {
  email: string;
  password: string;
}

/** POST /auth/register request body. */
export interface RegisterRequest {
  email: string;
  password: string;
  displayName?: string;
}

/** Token response from backend auth endpoints. */
export interface TokenResponse {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
}

/** User info from backend. */
export interface UserResponse {
  id: number;
  email: string;
  displayName: string | null;
  isActive: boolean;
  createdAt: string;
}

/** Subscription response from /me/subscriptions. */
export interface SubscriptionResponse {
  id: number;
  keywordId: number;
  keyword: string;
  category: string;
  createdAt: string;
}

/** GET /me/subscriptions response wrapper. */
export interface SubscriptionListResponse {
  items: SubscriptionResponse[];
}

/** Watchlist item response from /me/watchlist. */
export interface WatchlistResponse {
  id: number;
  newsArticleId: number;
  titleOriginal: string;
  url: string;
  source: string;
  publishedAt: string | null;
  createdAt: string;
}

/** GET /me/watchlist response wrapper. */
export interface WatchlistListResponse {
  items: WatchlistResponse[];
  total: number;
  page: number;
  perPage: number;
  totalPages: number;
}
