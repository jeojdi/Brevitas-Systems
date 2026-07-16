(function(){
  try {
    var saved = localStorage.getItem('theme');
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
    } else {
      document.documentElement.setAttribute('data-theme', 'dark');
    }

    // Respect reduced-motion preference or saved override
    var savedReduce = localStorage.getItem('reduceMotion');
    var prefersReduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (savedReduce === '1' || (!savedReduce && prefersReduce)) {
      document.documentElement.setAttribute('data-reduce-motion', 'true');
    } else {
      document.documentElement.removeAttribute('data-reduce-motion');
    }
  } catch (e) {
    // fail silently
  }

  // Most marketing pages are static files served through Next.js rewrites. Load the
  // shared analytics bootstrap here so they all receive the same privacy behavior.
  if (!document.querySelector('script[data-brevitas-analytics]')) {
    var analytics = document.createElement('script');
    analytics.src = '/analytics.js';
    analytics.defer = true;
    analytics.dataset.brevitasAnalytics = 'true';
    document.head.appendChild(analytics);
  }
})();
