import React, { useState, useEffect, useRef } from 'react';
import {
  Upload,
  Database,
  Send,
  Trash2,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  FileSpreadsheet,
  Layers,
  Sparkles,
  RefreshCw,
  PlusCircle,
  X,
  Filter,
} from 'lucide-react';
import {
  UploadDataResponse,
  ChatMessage,
  HealthResponse,
} from './types';
import {
  checkHealth,
  uploadData,
  askData,
  clearData,
} from './api';

const ALLOWED_EXTENSIONS = [
  'csv',
  'xlsx',
  'xls',
  'db',
  'sqlite',
  'sqlite3',
  'sql',
  'parquet',
  'json',
  'jsonl',
  'ndjson',
  'tsv',
  'tab',
  'txt',
];

const ACCEPT_STRING =
  '.csv, .xlsx, .xls, .db, .sqlite, .sqlite3, .sql, .parquet, .json, .jsonl, .ndjson, .tsv, .tab, .txt';

export const App: React.FC = () => {
  // Application State
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [session, setSession] = useState<UploadDataResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);

  // Chat / Query State
  const [question, setQuestion] = useState('');
  const [selectedTable, setSelectedTable] = useState<string>('all');
  const [isAsking, setIsAsking] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [expandedSchemas, setExpandedSchemas] = useState<Record<string, boolean>>({});

  const chatBottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const modalFileInputRef = useRef<HTMLInputElement | null>(null);

  // Initial Health Check
  useEffect(() => {
    fetchHealth();
  }, []);

  // Auto-scroll chat to bottom
  useEffect(() => {
    if (messages.length > 0) {
      chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, isAsking]);

  const fetchHealth = async () => {
    try {
      const res = await checkHealth();
      setHealth(res);
      setGlobalError(null);
    } catch (err: any) {
      setHealth(null);
      setGlobalError(err.message || "Couldn't reach the backend server.");
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
      setUploadError(null);
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const file = e.dataTransfer.files[0];
      const ext = file.name.split('.').pop()?.toLowerCase();
      if (ALLOWED_EXTENSIONS.includes(ext || '')) {
        setSelectedFile(file);
        setUploadError(null);
      } else {
        setUploadError(
          'Please select a supported database or data file (.db, .sqlite, .sql, .parquet, .json, .csv, .xlsx, .tsv).'
        );
      }
    }
  };

  const handleUpload = async (isAppending: boolean = false) => {
    if (!selectedFile) return;
    setIsUploading(true);
    setUploadError(null);
    setGlobalError(null);

    try {
      const sessionId = isAppending && session ? session.session_id : undefined;
      const res = await uploadData(selectedFile, sessionId);
      setSession(res);
      setSelectedFile(null);
      setShowAddModal(false);
    } catch (err: any) {
      setUploadError(err.message || 'Upload failed.');
    } finally {
      setIsUploading(false);
    }
  };

  const handleAsk = async (promptQuestion?: string) => {
    const q = (promptQuestion || question).trim();
    if (!q || !session || isAsking) return;

    const targetTableValue = selectedTable === 'all' ? undefined : selectedTable;
    const targetTableObj = session.tables.find((t) => t.table_name === targetTableValue);
    const targetDisplay = targetTableObj
      ? targetTableObj.original_filename || targetTableObj.table_name
      : 'All Session Tables';

    const messageId = String(Date.now());
    const newMsg: ChatMessage = {
      id: messageId,
      question: q,
      target_table: targetTableValue || 'all',
      target_table_name: targetDisplay,
      timestamp: new Date(),
      isLoading: true,
    };

    setMessages((prev) => [...prev, newMsg]);
    setQuestion('');
    setIsAsking(true);
    setGlobalError(null);

    try {
      const res = await askData(session.session_id, q, targetTableValue);
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === messageId
            ? {
                ...msg,
                answer: res.answer,
                isLoading: false,
              }
            : msg
        )
      );
    } catch (err: any) {
      // If session expired or missing (404), reset session automatically
      if (err.status === 404) {
        setGlobalError('Session expired or not found. Returning to upload screen.');
        setSession(null);
        setMessages([]);
      } else {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === messageId
              ? {
                  ...msg,
                  error: err.message || 'Query failed to execute.',
                  isLoading: false,
                }
              : msg
          )
        );
      }
    } finally {
      setIsAsking(false);
    }
  };

  const handleClearSession = async () => {
    if (!session) return;
    try {
      await clearData(session.session_id);
    } catch (err) {
      console.warn('Error during clear-data call:', err);
    } finally {
      setSession(null);
      setMessages([]);
      setSelectedFile(null);
      setUploadError(null);
      setGlobalError(null);
      setSelectedTable('all');
    }
  };

  const toggleTableSchema = (tableName: string) => {
    setExpandedSchemas((prev) => ({ ...prev, [tableName]: !prev[tableName] }));
  };

  const totalRowCount = session
    ? session.tables.reduce((acc, t) => acc + (t.row_count || 0), 0)
    : 0;

  const sampleSuggestions = [
    'What is the total count of records?',
    'What are the key categories or types?',
    'Show top records sorted by value',
    'Summarize this dataset for me',
  ];

  return (
    <div className="app-container">
      {/* Top Header */}
      <header className="app-header">
        <div className="brand-logo">
          <div className="brand-icon-box">
            <Database size={22} color="white" />
          </div>
          <div>
            <h1 className="brand-title">Text-to-SQL RAG Studio</h1>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Universal Database & Data Intelligence (SQLite, SQL, Parquet, JSON, TSV, CSV, Excel)
            </p>
          </div>
        </div>

        <div className="header-status">
          {session && (
            <div className="health-badge" style={{ color: '#a5b4fc' }}>
              <Layers size={14} />
              <span>
                {session.tables.length} Table{session.tables.length === 1 ? '' : 's'} •{' '}
                {totalRowCount.toLocaleString()} Rows
              </span>
            </div>
          )}

          <div className="health-badge" title="Backend Connection Health">
            <span
              className={`health-dot ${
                health?.status === 'healthy'
                  ? 'healthy'
                  : health
                  ? 'degraded'
                  : 'offline'
              }`}
            />
            <span>
              {health?.status === 'healthy'
                ? 'Connected'
                : health
                ? 'Degraded'
                : 'Server Offline'}
            </span>
          </div>

          <button
            className="btn-secondary"
            style={{ padding: '0.35rem 0.6rem' }}
            onClick={fetchHealth}
            title="Refresh Connection"
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </header>

      {/* Global Error Banner */}
      {globalError && (
        <div className="alert-banner alert-danger">
          <AlertCircle size={18} />
          <span>{globalError}</span>
        </div>
      )}

      {/* Main View: Upload Screen vs Multi-Table Question Screen */}
      <main className="main-view">
        {!session ? (
          /* ---------------- INITIAL UPLOAD SCREEN ---------------- */
          <div className="upload-hero-card">
            <Layers className="upload-icon" />
            <h2 style={{ fontSize: '1.6rem', fontWeight: 700, marginBottom: '0.5rem' }}>
              Upload Database & Data Files
            </h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.92rem', maxWidth: 520, margin: '0 auto' }}>
              Upload SQLite databases (<code>.db</code>, <code>.sqlite</code>), SQL scripts (<code>.sql</code>), Parquet (<code>.parquet</code>), JSON (<code>.json</code>, <code>.jsonl</code>), TSV, CSV, or Excel. Tables are ingested into PostgreSQL for natural language querying and joins.
            </p>

            <div
              className="upload-dropzone"
              onDragOver={(e) => e.preventDefault()}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                type="file"
                ref={fileInputRef}
                style={{ display: 'none' }}
                accept={ACCEPT_STRING}
                onChange={handleFileSelect}
              />
              <FileSpreadsheet className="upload-icon" style={{ width: 42, height: 42 }} />
              {selectedFile ? (
                <div>
                  <p style={{ fontWeight: 600, color: 'var(--accent-primary)', fontSize: '1.05rem' }}>
                    {selectedFile.name}
                  </p>
                  <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
                    {(selectedFile.size / 1024).toFixed(1)} KB — Click button below to initialize
                  </p>
                </div>
              ) : (
                <div>
                  <p style={{ fontWeight: 500, fontSize: '0.95rem' }}>
                    Drag & drop your database or data file here, or{' '}
                    <span style={{ color: 'var(--accent-primary)', textDecoration: 'underline' }}>browse</span>
                  </p>
                  <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: '0.5rem' }}>
                    Supports SQLite (.db, .sqlite), SQL (.sql), Parquet (.parquet), JSON (.json, .jsonl), TSV, CSV, and Excel up to 10MB
                  </p>
                </div>
              )}
            </div>

            {uploadError && (
              <div className="alert-banner alert-danger" style={{ textAlign: 'left' }}>
                <AlertCircle size={18} />
                <span>{uploadError}</span>
              </div>
            )}

            <button
              className="btn-primary"
              style={{ width: '100%', padding: '0.85rem' }}
              disabled={!selectedFile || isUploading}
              onClick={() => handleUpload(false)}
            >
              {isUploading ? (
                <>
                  <div className="spinner" />
                  <span>Parsing & Loading into PostgreSQL...</span>
                </>
              ) : (
                <>
                  <Upload size={18} />
                  <span>Upload & Start Session</span>
                </>
              )}
            </button>
          </div>
        ) : (
          /* ---------------- MULTI-TABLE QUESTION INTERFACE ---------------- */
          <>
            {/* Session Deck & Table Cards */}
            <div className="session-summary-bar">
              <div className="summary-header">
                <div className="summary-title">
                  <Database size={18} style={{ color: 'var(--accent-primary)' }} />
                  <span>Active Session Tables ({session.tables.length})</span>
                </div>

                <div className="summary-actions">
                  <button
                    className="btn-secondary"
                    onClick={() => {
                      setSelectedFile(null);
                      setUploadError(null);
                      setShowAddModal(true);
                    }}
                    title="Upload another database or data file into this session"
                  >
                    <PlusCircle size={15} style={{ color: '#818cf8' }} />
                    <span>Add Another File</span>
                  </button>

                  <button
                    className="btn-danger-outline"
                    onClick={handleClearSession}
                    title="Drop all session tables and reset"
                  >
                    <Trash2 size={14} />
                    <span>Clear Session</span>
                  </button>
                </div>
              </div>

              {/* Table Cards Deck */}
              <div className="tables-grid">
                {session.tables.map((table) => {
                  const isExpanded = expandedSchemas[table.table_name];
                  return (
                    <div key={table.table_name} className="table-card">
                      <div className="card-top">
                        <div className="card-filename">
                          <FileSpreadsheet size={16} style={{ color: '#38bdf8' }} />
                          <span>{table.original_filename || table.table_name}</span>
                        </div>
                        <span className="meta-badge">
                          {table.row_count.toLocaleString()} rows
                        </span>
                      </div>

                      <div className="card-pg-name" title={table.table_name}>
                        {table.table_name}
                      </div>

                      <button
                        className="schema-toggle-btn"
                        onClick={() => toggleTableSchema(table.table_name)}
                      >
                        {isExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                        <span>{table.columns.length} columns {isExpanded ? '(Hide)' : '(View)'}</span>
                      </button>

                      {isExpanded && (
                        <div className="card-schema-pills">
                          {table.columns.map((col, cIdx) => (
                            <span key={cIdx} className="schema-pill">
                              {col.name}
                              <span className="schema-type">({col.type})</span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Chat History Container */}
            <div className="chat-container">
              {messages.length === 0 && (
                <div
                  style={{
                    textAlign: 'center',
                    padding: '3rem 1.5rem',
                    color: 'var(--text-secondary)',
                  }}
                >
                  <Sparkles size={36} style={{ color: 'var(--accent-primary)', marginBottom: '0.85rem' }} />
                  <h3 style={{ color: 'var(--text-primary)', fontSize: '1.2rem', marginBottom: '0.5rem' }}>
                    {session.tables.length > 1
                      ? `${session.tables.length} tables ready for querying & joins`
                      : 'Dataset ready for questions'}
                  </h3>
                  <p style={{ fontSize: '0.9rem', maxWidth: 520, margin: '0 auto' }}>
                    Ask any natural language question across your uploaded dataset(s). The AI will analyze
                    the underlying data and deliver clear, direct answers.
                  </p>
                </div>
              )}

              {messages.map((msg) => (
                <div key={msg.id} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                  {/* User Question */}
                  <div className="chat-bubble user">
                    <div className="bubble-header">
                      <span className="bubble-scope-badge">
                        <Filter size={11} />
                        <span>{msg.target_table_name || 'All Session Tables'}</span>
                      </span>
                      <span>{msg.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                    </div>
                    <p style={{ fontSize: '1.02rem', fontWeight: 500 }}>{msg.question}</p>
                  </div>

                  {/* Assistant Answer */}
                  <div className="chat-bubble">
                    <div className="bubble-header">
                      <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                        <Database size={16} style={{ color: 'var(--accent-primary)' }} />
                        <span>Answer</span>
                      </span>
                    </div>

                    {msg.isLoading ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', padding: '1.25rem 0' }}>
                        <div className="spinner" />
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                          Analyzing database records...
                        </span>
                      </div>
                    ) : msg.error ? (
                      <div className="alert-banner alert-danger">
                        <AlertCircle size={16} />
                        <span>{msg.error}</span>
                      </div>
                    ) : (
                      /* Plain Natural Language Answer */
                      <div className="answer-text">{msg.answer}</div>
                    )}
                  </div>
                </div>
              ))}
              <div ref={chatBottomRef} />
            </div>

            {/* Bottom Sticky Query Input & Table Selector */}
            <div className="input-dock">
              <div className="dock-controls">
                {/* Table Scope Selector */}
                <div className="table-select-wrapper">
                  <Filter size={13} />
                  <span>Target Scope:</span>
                  <select
                    className="table-dropdown"
                    value={selectedTable}
                    onChange={(e) => setSelectedTable(e.target.value)}
                    disabled={isAsking}
                  >
                    <option value="all">
                      ✨ All Tables ({session.tables.length}) - Auto Route & Joins
                    </option>
                    {session.tables.map((t) => (
                      <option key={t.table_name} value={t.table_name}>
                        📊 {t.original_filename || t.table_name} ({t.row_count} rows)
                      </option>
                    ))}
                  </select>
                </div>

                {/* Suggestions Row */}
                {messages.length === 0 && (
                  <div className="suggestions-row">
                    {sampleSuggestions.map((s, idx) => (
                      <button
                        key={idx}
                        className="suggestion-chip"
                        onClick={() => handleAsk(s)}
                        disabled={isAsking}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <form
                className="input-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  handleAsk();
                }}
              >
                <input
                  type="text"
                  className="query-input"
                  placeholder={
                    selectedTable === 'all'
                      ? "Ask a question across all session tables (e.g. 'What is the top product by sales?')"
                      : `Ask a question about ${
                          session.tables.find((t) => t.table_name === selectedTable)?.original_filename || 'selected table'
                        }...`
                  }
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  disabled={isAsking}
                />
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={!question.trim() || isAsking}
                  title="Run Query (Enter)"
                >
                  {isAsking ? (
                    <div className="spinner" />
                  ) : (
                    <>
                      <Send size={16} />
                      <span>Ask</span>
                    </>
                  )}
                </button>
              </form>
            </div>
          </>
        )}
      </main>

      {/* Modal: Add Additional File to Active Session */}
      {showAddModal && session && (
        <div className="modal-overlay" onClick={() => setShowAddModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3 style={{ fontSize: '1.2rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <PlusCircle size={20} style={{ color: 'var(--accent-primary)' }} />
                <span>Add Database or Data File to Session</span>
              </h3>
              <button
                className="btn-secondary"
                style={{ padding: '0.3rem', border: 'none' }}
                onClick={() => setShowAddModal(false)}
              >
                <X size={18} />
              </button>
            </div>

            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
              Upload another SQLite database (<code>.db</code>), SQL script (<code>.sql</code>), Parquet (<code>.parquet</code>), JSON (<code>.json</code>), TSV, CSV, or Excel file to session <code>{session.session_id.slice(0, 8)}...</code>.
            </p>

            <div
              className="upload-dropzone"
              style={{ margin: '0.5rem 0' }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={handleDrop}
              onClick={() => modalFileInputRef.current?.click()}
            >
              <input
                type="file"
                ref={modalFileInputRef}
                style={{ display: 'none' }}
                accept={ACCEPT_STRING}
                onChange={handleFileSelect}
              />
              <FileSpreadsheet className="upload-icon" style={{ width: 36, height: 36 }} />
              {selectedFile ? (
                <div>
                  <p style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>
                    {selectedFile.name}
                  </p>
                  <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                    {(selectedFile.size / 1024).toFixed(1)} KB — Ready to upload
                  </p>
                </div>
              ) : (
                <div>
                  <p style={{ fontWeight: 500, fontSize: '0.9rem' }}>
                    Select or drag & drop another database or tabular file
                  </p>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
                    Supports .db, .sqlite, .sql, .parquet, .json, .tsv, .csv, .xlsx (up to 10MB)
                  </p>
                </div>
              )}
            </div>

            {uploadError && (
              <div className="alert-banner alert-danger" style={{ textAlign: 'left', margin: 0 }}>
                <AlertCircle size={16} />
                <span>{uploadError}</span>
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.75rem', marginTop: '0.5rem' }}>
              <button
                className="btn-secondary"
                onClick={() => setShowAddModal(false)}
                disabled={isUploading}
              >
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!selectedFile || isUploading}
                onClick={() => handleUpload(true)}
              >
                {isUploading ? (
                  <>
                    <div className="spinner" />
                    <span>Uploading File...</span>
                  </>
                ) : (
                  <>
                    <Upload size={16} />
                    <span>Upload & Add File</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
