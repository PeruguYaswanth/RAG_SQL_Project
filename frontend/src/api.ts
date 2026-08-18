import {
  UploadDataResponse,
  AskDataResponse,
  DataStatusResponse,
  ClearDataResponse,
  HealthResponse,
} from './types';

export const API_BASE =
  (import.meta.env.VITE_API_BASE as string) || 'http://127.0.0.1:8000';

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let errorMessage = `Request failed with status ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === 'string') {
        errorMessage = data.detail;
      } else if (Array.isArray(data.detail)) {
        errorMessage = data.detail.map((err: any) => err.msg || JSON.stringify(err)).join(', ');
      } else if (data.message) {
        errorMessage = data.message;
      }
    } catch {
      // Non-JSON error body
      const text = await response.text().catch(() => '');
      if (text) errorMessage = text;
    }
    throw new ApiError(errorMessage, response.status);
  }
  return response.json() as Promise<T>;
}

function handleFetchError(error: unknown): never {
  if (error instanceof ApiError) {
    throw error;
  }
  if (error instanceof Error && (error.message.includes('fetch') || error.name === 'TypeError')) {
    throw new ApiError(
      "Couldn't reach the server, it may be starting up or offline.",
      0
    );
  }
  throw new ApiError(
    error instanceof Error ? error.message : 'An unexpected error occurred.',
    0
  );
}

export async function checkHealth(): Promise<HealthResponse> {
  try {
    const res = await fetch(`${API_BASE}/api/health`, {
      method: 'GET',
    });
    return await handleResponse<HealthResponse>(res);
  } catch (err) {
    return handleFetchError(err);
  }
}

export async function uploadData(
  file: File,
  sessionId?: string
): Promise<UploadDataResponse> {
  try {
    const formData = new FormData();
    formData.append('file', file);
    if (sessionId) {
      formData.append('session_id', sessionId);
    }

    const res = await fetch(`${API_BASE}/api/upload-data`, {
      method: 'POST',
      body: formData,
    });
    return await handleResponse<UploadDataResponse>(res);
  } catch (err) {
    return handleFetchError(err);
  }
}

export async function askData(
  sessionId: string,
  question: string,
  tableName?: string
): Promise<AskDataResponse> {
  try {
    const payload: Record<string, any> = {
      session_id: sessionId,
      question: question.trim(),
    };
    if (tableName && tableName !== 'all') {
      payload.table_name = tableName;
    }

    const res = await fetch(`${API_BASE}/api/ask-data`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    return await handleResponse<AskDataResponse>(res);
  } catch (err) {
    return handleFetchError(err);
  }
}

export async function getDataStatus(
  sessionId: string
): Promise<DataStatusResponse> {
  try {
    const url = `${API_BASE}/api/data-status?session_id=${encodeURIComponent(sessionId)}`;
    const res = await fetch(url, {
      method: 'GET',
    });
    return await handleResponse<DataStatusResponse>(res);
  } catch (err) {
    return handleFetchError(err);
  }
}

export async function clearData(sessionId: string): Promise<ClearDataResponse> {
  try {
    const formData = new FormData();
    formData.append('session_id', sessionId);

    const res = await fetch(`${API_BASE}/api/clear-data`, {
      method: 'POST',
      body: formData,
    });
    return await handleResponse<ClearDataResponse>(res);
  } catch (err) {
    return handleFetchError(err);
  }
}
