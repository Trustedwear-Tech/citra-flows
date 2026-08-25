const express = require('express');
const router = express.Router();
const { authenticateToken, requireAdmin } = require('../middleware/authMiddleware');

// Coupon system disabled — on-premises license model, coupons not applicable

router.post('/validate', authenticateToken, (req, res) => {
  res.status(410).json({ success: false, message: 'Coupon system not available.' });
});

router.post('/apply', authenticateToken, (req, res) => {
  res.status(410).json({ success: false, message: 'Coupon system not available.' });
});

router.post('/create', authenticateToken, requireAdmin, (req, res) => {
  res.status(410).json({ success: false, message: 'Coupon system not available.' });
});

router.post('/check-access', authenticateToken, (req, res) => {
  res.json({ has_access: true, access_type: 'license' });
});

module.exports = router;

