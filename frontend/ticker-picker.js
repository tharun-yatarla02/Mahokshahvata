// Ticker picker: a themed autocomplete dropdown for ticker inputs, replacing the
// browser's native <datalist> popup (unstyled, ignores the theme, and can run
// off-screen). Usage: attachTickerPicker(document.getElementById('tradeTicker'));
// Picking a ticker sets the input's value and fires a 'change' event.
(function () {
  // Suggestions come from /momentum, which covers the ~3000 largest US stocks.
  // An empty box shows the largest companies; typing searches ticker, company
  // name and sector. Responses are cached per query for the page's lifetime.
  const SUGGESTION_COUNT = 20;
  const cache = new Map();

  function search(query) {
    const q = query.trim();
    if (!cache.has(q)) {
      const params = new URLSearchParams({ top: String(SUGGESTION_COUNT) });
      if (q) params.set('q', q); else params.set('sort', 'market_cap');
      cache.set(q, fetch(`${window.location.origin}/momentum?${params}`)
        .then((res) => (res.ok ? res.json() : { results: [] }))
        .then((data) => data.results || [])
        .catch(() => {
          cache.delete(q);  // let the next keystroke retry
          return [];
        }));
    }
    return cache.get(q);
  }

  let pickerCount = 0;

  window.attachTickerPicker = function attachTickerPicker(input) {
    if (!input) return;
    const listId = `ticker-picker-list-${++pickerCount}`;
    const listedId = input.getAttribute('list');
    input.removeAttribute('list');
    if (listedId) document.getElementById(listedId)?.remove();

    const wrapper = document.createElement('div');
    wrapper.className = 'ticker-picker';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    const list = document.createElement('ul');
    list.id = listId;
    list.className = 'ticker-picker-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    wrapper.appendChild(list);

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', listId);
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');

    let items = [];
    let activeIndex = -1;
    let showAll = false;
    let loading = false;
    let requestId = 0;
    let searchTimer = null;

    function message(text) {
      const li = document.createElement('li');
      li.className = 'ticker-picker-empty';
      li.textContent = text;
      list.appendChild(li);
    }

    function render() {
      list.innerHTML = '';
      if (!items.length) {
        const typed = input.value.trim().toUpperCase();
        if (loading) message('Searching…');
        else if (typed && !showAll) message(`No match in tracked stocks. Press Enter to use "${typed}".`);
        else message('Market data is still loading.');
      }
      const current = input.value.trim().toUpperCase();
      items.forEach((company, index) => {
        const li = document.createElement('li');
        li.id = `${listId}-${index}`;
        li.className = 'ticker-picker-option';
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', String(index === activeIndex));
        if (company.ticker === current) li.classList.add('current');

        const symbol = document.createElement('span');
        symbol.className = 'ticker-picker-symbol';
        symbol.textContent = company.ticker;
        const info = document.createElement('span');
        info.className = 'ticker-picker-info';
        const name = document.createElement('span');
        name.className = 'ticker-picker-name';
        name.textContent = company.name || company.ticker;
        const sector = document.createElement('span');
        sector.className = 'ticker-picker-sector';
        sector.textContent = company.sector || '';
        info.append(name, sector);
        const quote = document.createElement('span');
        quote.className = 'ticker-picker-quote';
        if (company.current_price != null) {
          const price = document.createElement('span');
          price.textContent = `$${Number(company.current_price).toFixed(2)}`;
          const change = document.createElement('small');
          const pct = Number(company.momentum_pct || 0);
          change.className = pct >= 0 ? 'positive' : 'negative';
          change.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}% 90d`;
          quote.append(price, change);
        }
        li.append(symbol, info, quote);
        li.addEventListener('mousedown', (event) => {
          event.preventDefault();  // keep focus in the input
          pick(company.ticker);
        });
        li.addEventListener('mousemove', () => setActive(index, false));
        list.appendChild(li);
      });
    }

    function position() {
      // Open upward when there isn't room below, so the list never runs off-screen.
      const rect = input.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      wrapper.classList.toggle('open-up', spaceBelow < 240 && rect.top > spaceBelow);
    }

    async function refresh() {
      const id = ++requestId;
      loading = true;
      render();
      const results = await search(showAll ? '' : input.value);
      if (id !== requestId) return;  // a newer keystroke won
      loading = false;
      items = results;
      activeIndex = -1;
      if (!list.hidden) render();
    }

    function open() {
      position();
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      refresh();
    }

    function close() {
      list.hidden = true;
      activeIndex = -1;
      showAll = false;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    }

    function setActive(index, scroll = true) {
      activeIndex = index;
      list.querySelectorAll('.ticker-picker-option').forEach((li, i) => {
        li.setAttribute('aria-selected', String(i === index));
      });
      const active = document.getElementById(`${listId}-${index}`);
      if (active) {
        input.setAttribute('aria-activedescendant', active.id);
        if (scroll) active.scrollIntoView({ block: 'nearest' });
      }
    }

    function pick(ticker) {
      input.value = ticker;
      close();
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    input.addEventListener('focus', () => {
      showAll = true;  // show the full list first; typing narrows it
      open();
    });
    input.addEventListener('click', () => { if (list.hidden) { showAll = true; open(); } });
    input.addEventListener('input', () => {
      showAll = false;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(open, 150);
    });
    input.addEventListener('blur', close);
    input.addEventListener('keydown', async (event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) open();
        if (!items.length) return;
        const step = event.key === 'ArrowDown' ? 1 : -1;
        setActive((activeIndex + step + items.length) % items.length);
      } else if (event.key === 'Enter' && !list.hidden) {
        event.preventDefault();
        const typed = input.value.trim();
        let choice = items[activeIndex];
        if (!choice && typed) {
          // Use results for exactly what's typed, even if the debounced search
          // hasn't come back yet: an exact ticker first, then a lone match.
          clearTimeout(searchTimer);
          const results = await search(typed);
          choice = results.find((r) => r.ticker === typed.toUpperCase()) || (results.length === 1 ? results[0] : null);
        }
        pick(choice ? choice.ticker : typed.toUpperCase());
      } else if (event.key === 'Escape' && !list.hidden) {
        event.preventDefault();
        close();
      }
    });
    window.addEventListener('resize', () => { if (!list.hidden) position(); });
  };
})();
