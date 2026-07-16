const analytics = () => window.brevitasAnalytics

export const capture = (event, properties = {}) => analytics()?.capture(event, properties)
export const identify = (userId, properties = {}) => analytics()?.identify(userId, properties)
export const resetAnalytics = () => analytics()?.reset()
