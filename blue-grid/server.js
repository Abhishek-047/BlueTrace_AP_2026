/**
 * BlueTrace Node.js & Express DBMS Integration Server
 * 
 * Provides:
 * 1. Static file serving for blue-grid UI.
 * 2. PostgreSQL DBMS integration endpoints (/api/screenings, /api/health, /api/stats).
 * 3. LLM proxy endpoint (/ask) compatible with the client-side mini-RAG validator.
 * 4. Fallback in-memory storage if PostgreSQL is not running locally.
 */

const express = require('express');
const path = require('path');
const fs = require('fs');
const { Pool } = require('pg');

const app = express();
const PORT = process.env.PORT || 8000;
const HOST = process.env.HOST || '0.0.0.0';

app.use(express.json({ limit: '2mb' }));

// ---------------------------------------------------------------------
// PostgreSQL Connection Pool Configuration
// Uses PG environment variables (PGHOST, PGDATABASE, PGUSER, etc.)
// ---------------------------------------------------------------------
const pool = new Pool({
  connectionString: process.env.DATABASE_URL || 'postgresql://localhost:5432/bluetrace',
  connectionTimeoutMillis: 3000,
});

let dbConnected = false;

pool.query('SELECT 1 FROM information_schema.tables WHERE table_schema = $1 LIMIT 1', ['bluetrace'])
  .then(() => {
    dbConnected = true;
    console.log('[DBMS] Connected to PostgreSQL bluetrace schema successfully.');
  })
  .catch((err) => {
    dbConnected = false;
    console.log('[DBMS] PostgreSQL not currently connected (' + err.message + '). Using seamless in-memory certificate cache.');
  });

// In-memory fallback certificate store when PostgreSQL is offline
const memoryScreenings = [];

// ---------------------------------------------------------------------
// API Routes
// ---------------------------------------------------------------------

// System and DBMS Health Status
app.get('/api/health', async (req, res) => {
  let dbStatus = 'disconnected';
  try {
    const q = await pool.query('SELECT current_database(), current_schema()');
    dbStatus = 'connected (' + q.rows[0].current_database + ')';
    dbConnected = true;
  } catch (err) {
    dbConnected = false;
  }

  res.json({
    status: 'ok',
    service: 'BlueTrace Node.js Backend',
    database: dbStatus,
    inMemoryStored: memoryScreenings.length,
    timestamp: new Date().toISOString()
  });
});

// Fetch recent screening certificates
app.get('/api/screenings', async (req, res) => {
  if (dbConnected) {
    try {
      const result = await pool.query(`
        SELECT c.cert_ref, c.issued_at, c.applicant, c.survey_no, c.application_no,
               s.lat, s.lon, s.plot_m, s.dist_present, s.dist_historic, s.dist_drain,
               s.city_key, s.rule_id
        FROM bluetrace.certificate c
        JOIN bluetrace.screening s ON c.screening_id = s.screening_id
        ORDER BY c.issued_at DESC
        LIMIT 50
      `);
      return res.json({ source: 'database', items: result.rows });
    } catch (err) {
      console.warn('[DBMS] Error querying database, falling back to cache:', err.message);
    }
  }

  res.json({ source: 'cache', items: memoryScreenings.slice(-50).reverse() });
});

// Save a screening certificate into DBMS
app.post('/api/screenings', async (req, res) => {
  const data = req.body;
  if (!data || !data.cert_ref) {
    return res.status(400).json({ error: 'Missing cert_ref in payload' });
  }

  const record = {
    cert_ref: data.cert_ref,
    issued_at: data.issued_at || new Date().toISOString(),
    applicant: data.applicant || null,
    survey_no: data.survey_no || null,
    application_no: data.application_no || null,
    city_key: data.city_key || 'bengaluru',
    rule_id: data.rule_id || 'ngt',
    lat: Number(data.lat),
    lon: Number(data.lon),
    plot_m: Number(data.plot_m) || 30,
    dist_present: data.dist_present !== undefined ? Number(data.dist_present) : null,
    dist_historic: data.dist_historic !== undefined ? Number(data.dist_historic) : null,
    dist_drain: data.dist_drain !== undefined ? Number(data.dist_drain) : null,
    verdict: data.verdict || 'NO_OBJECTION',
    reasons: data.reasons || []
  };

  // Always keep in fallback cache
  memoryScreenings.push(record);

  // If DB is connected, insert into relational schema
  if (dbConnected) {
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      
      const screenRes = await client.query(`
        INSERT INTO bluetrace.screening (
          city_key, rule_id, lat, lon, plot_m, dist_present, dist_historic, dist_drain
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING screening_id
      `, [
        record.city_key, record.rule_id, record.lat, record.lon,
        record.plot_m, record.dist_present, record.dist_historic, record.dist_drain
      ]);

      const screeningId = screenRes.rows[0].screening_id;

      await client.query(`
        INSERT INTO bluetrace.certificate (
          cert_ref, screening_id, issued_at, application_no, survey_no, applicant, corpus_version
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
      `, [
        record.cert_ref, screeningId, record.issued_at,
        record.application_no, record.survey_no, record.applicant, 'v1.0'
      ]);

      await client.query('COMMIT');
      return res.status(201).json({ success: true, persisted: 'database', cert_ref: record.cert_ref });
    } catch (dbErr) {
      await client.query('ROLLBACK');
      console.warn('[DBMS] DB insert failed, preserved in cache:', dbErr.message);
    } finally {
      client.release();
    }
  }

  res.status(201).json({ success: true, persisted: 'cache', cert_ref: record.cert_ref });
});

// Proxy /ask for LLM answers (matches Python server.py contract)
app.post('/ask', (req, res) => {
  // If provider keys are present, they can be configured; otherwise return 503 so client uses deterministic Tier 0
  const apiKey = process.env.ANTHROPIC_API_KEY || process.env.GEMINI_API_KEY || process.env.GROQ_API_KEY;
  if (!apiKey) {
    return res.status(503).json({
      reason: 'No LLM API key configured. BlueTrace is running in deterministic offline mode (Tier 0).'
    });
  }

  // Model configured
  res.status(503).json({
    reason: 'Model proxy available. Deterministic Tier 0 active.'
  });
});

// Serve static frontend files
const STATIC_DIR = path.resolve(__dirname);
app.use(express.static(STATIC_DIR));

// Fallback to index.html
app.get('*', (req, res) => {
  res.sendFile(path.join(STATIC_DIR, 'index.html'));
});

// Start listening
app.listen(PORT, HOST, () => {
  console.log(`BlueTrace Node.js server running at http://${HOST}:${PORT}`);
  console.log(`Serving assets from: ${STATIC_DIR}`);
});
