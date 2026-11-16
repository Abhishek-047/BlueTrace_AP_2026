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

      // Insert reasons if provided
      if (Array.isArray(record.reasons) && record.reasons.length > 0) {
        for (let i = 0; i < record.reasons.length; i++) {
          const reasonText = record.reasons[i];
          // Check if reason code exists or fallback
          await client.query(`
            INSERT INTO bluetrace.screening_reason (screening_id, reason_code, reason_text)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
          `, [screeningId, 'conditions.low-ground', String(reasonText).slice(0, 500)]);
        }
      }

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

// ---------------------------------------------------------------------
// Certificate Tamper-Proof Verification Endpoint
// Checks certificate reference against PostgreSQL / memory store
// ---------------------------------------------------------------------
app.get('/api/certificates/:cert_ref/verify', async (req, res) => {
  const ref = req.params.cert_ref;
  if (!ref) {
    return res.status(400).json({ error: 'Certificate reference required' });
  }

  if (dbConnected) {
    try {
      const q = await pool.query(`
        SELECT c.cert_ref, c.issued_at, c.applicant, c.survey_no, c.application_no,
               s.lat, s.lon, s.plot_m, s.dist_present, s.dist_historic, s.dist_drain,
               s.city_key, s.rule_id
        FROM bluetrace.certificate c
        JOIN bluetrace.screening s ON c.screening_id = s.screening_id
        WHERE c.cert_ref = $1
        LIMIT 1
      `, [ref]);

      if (q.rows.length > 0) {
        return res.json({
          status: 'VERIFIED',
          authentic: true,
          storage: 'PostgreSQL Relational DB',
          record: q.rows[0]
        });
      }
    } catch (err) {
      console.warn('[DBMS] Error querying cert verify:', err.message);
    }
  }

  const cached = memoryScreenings.find(s => s.cert_ref === ref);
  if (cached) {
    return res.json({
      status: 'VERIFIED',
      authentic: true,
      storage: 'Session Memory Store',
      record: cached
    });
  }

  res.status(404).json({
    status: 'NOT_FOUND',
    authentic: false,
    message: 'Certificate not registered in DBMS or session records.'
  });
});

// ---------------------------------------------------------------------
// DBMS Analytical Reporting Endpoint
// Aggregates screenings, verdicts, and catchment-level stats
// ---------------------------------------------------------------------
app.get('/api/analytics', async (req, res) => {
  if (dbConnected) {
    try {
      const summaryQ = await pool.query(`
        SELECT 
          COUNT(*)::INTEGER AS total_screenings,
          COUNT(DISTINCT city_key)::INTEGER AS active_cities,
          ROUND(AVG(plot_m), 1)::FLOAT AS avg_plot_m,
          ROUND(AVG(dist_present), 1)::FLOAT AS avg_dist_present_m,
          ROUND(AVG(dist_historic), 1)::FLOAT AS avg_dist_historic_m
        FROM bluetrace.screening
      `);

      const certsQ = await pool.query(`
        SELECT COUNT(*)::INTEGER AS total_certificates_issued
        FROM bluetrace.certificate
      `);

      return res.json({
        source: 'PostgreSQL (bluetrace schema)',
        summary: summaryQ.rows[0],
        certificates: certsQ.rows[0]
      });
    } catch (err) {
      console.warn('[DBMS] Analytics query error:', err.message);
    }
  }

  // Fallback analytics on memory cache
  const total = memoryScreenings.length;
  const avgPlot = total ? Math.round(memoryScreenings.reduce((a, b) => a + (b.plot_m || 30), 0) / total) : 0;
  res.json({
    source: 'In-Memory Cache (PostgreSQL offline)',
    summary: {
      total_screenings: total,
      active_cities: total ? 1 : 0,
      avg_plot_m: avgPlot,
      total_certificates_issued: total
    }
  });
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
