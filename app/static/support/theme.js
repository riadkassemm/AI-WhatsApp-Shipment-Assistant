(function () {
  try {
    const saved = localStorage.getItem('shipment-support-theme');
    const preferred = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : preferred;
  } catch (_) {
    document.documentElement.dataset.theme = 'light';
  }
}());
