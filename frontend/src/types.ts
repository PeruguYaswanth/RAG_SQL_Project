export interface ColumnInfo {
  name: string;
  type: string;
}

export interface TableInfo {
  table_name: string;
  columns: ColumnInfo[];
  row_count: number;
  original_filename: string;
}

export interface UploadDataResponse {
  session_id: string;
  tables: TableInfo[];
  // Backward compatibility fields
  table_name?: string;
  columns?: ColumnInfo[];
  row_count?: number;
}

export interface AskDataRequest {
  session_id: string;
  question: string;
  table_name?: string;
}

export interface AskDataResponse {
  answer: string;
  sql_query: string;
  row_count: number;
  sample_rows: Record<string, any>[];
}

export interface DataStatusResponse {
  session_id: string;
  tables: TableInfo[];
  table_name?: string;
  columns?: ColumnInfo[];
  row_count?: number;
  uploaded_at: string;
}

export interface ClearDataResponse {
  status: string;
}

export interface HealthResponse {
  status: string;
  groq_configured: boolean;
  database_configured: boolean;
}

export interface ChatMessage {
  id: string;
  question: string;
  target_table?: string; // Table name targeted, or 'all'
  target_table_name?: string; // Display friendly filename
  answer?: string;
  sql_query?: string;
  row_count?: number;
  sample_rows?: Record<string, any>[];
  timestamp: Date;
  isLoading?: boolean;
  error?: string;
}
