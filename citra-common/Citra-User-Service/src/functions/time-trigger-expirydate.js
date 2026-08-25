// DEPRECATED: This was an Azure Timer Trigger function for SQL Database
// Now using MongoDB with scheduled jobs in src/jobs/

const handler = async (myTimer, context) => {
    console.warn('time-trigger-expirydate is deprecated. Use MongoDB scheduled jobs instead.');
    return {
        status: 410,
        body: JSON.stringify({ 
            error: 'This endpoint is deprecated. Subscription expiry is now handled by MongoDB scheduled jobs.' 
        })
    };
};

module.exports = handler;



