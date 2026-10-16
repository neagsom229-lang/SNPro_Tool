document.addEventListener('DOMContentLoaded', () => {
  // Animated counters
  const counters = document.querySelectorAll('.stat-number');
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const target = parseFloat(entry.target.dataset.target);
        const suffix = entry.target.dataset.suffix || '';
        animateCounter(entry.target, target, suffix);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.3 });

  counters.forEach(c => observer.observe(c));

  function animateCounter(el, target, suffix) {
    if (!target) { el.textContent = '0' + suffix; return; }
    let current = 0;
    const step = target / 30;
    const timer = setInterval(() => {
      current += step;
      if (current >= target) {
        el.textContent = target + suffix;
        clearInterval(timer);
      } else {
        el.textContent = Math.floor(current) + suffix;
      }
    }, 30);
  }

  // Client-side search & category filter
  const searchInput = document.getElementById('toolSearch');
  const categoryBtns = document.querySelectorAll('.category-btn');
  const toolItems = document.querySelectorAll('.tool-item');
  const noToolsFound = document.getElementById('noToolsFound');

  let currentCategory = 'all';

  function filterTools() {
    const query = (searchInput ? searchInput.value : '').toLowerCase().trim();
    let visibleCount = 0;

    toolItems.forEach(item => {
      const name = item.dataset.name || '';
      const desc = item.dataset.desc || '';
      const cat = item.dataset.category || '';
      const isAi = item.dataset.ai === 'true';

      const matchesQuery = !query || name.includes(query) || desc.includes(query);
      let matchesCat = true;
      if (currentCategory === 'ai') {
        matchesCat = isAi;
      } else if (currentCategory !== 'all') {
        matchesCat = (cat === currentCategory);
      }

      if (matchesQuery && matchesCat) {
        item.classList.remove('d-none');
        visibleCount++;
      } else {
        item.classList.add('d-none');
      }
    });

    if (noToolsFound) {
      if (visibleCount === 0) {
        noToolsFound.classList.remove('d-none');
      } else {
        noToolsFound.classList.add('d-none');
      }
    }
  }

  if (searchInput) {
    searchInput.addEventListener('input', filterTools);
  }

  categoryBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      categoryBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentCategory = btn.dataset.category || 'all';
      filterTools();
    });
  });
});
