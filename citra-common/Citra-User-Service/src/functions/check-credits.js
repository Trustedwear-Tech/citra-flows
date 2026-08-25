/**
 * Check Credits — License model: always returns sufficient.
 * Usage tracking continues but no enforcement.
 */
const handler = async (req, res) => {
  return res.status(200).json({
    success: true,
    sufficient: true,
    balance: 999999999,
    required: req.body?.required_amount || req.body?.estimated_cost || 0,
    message: 'Unlimited (license model)'
  });
};

module.exports = handler;


