document.addEventListener('DOMContentLoaded', () => {
  // Prevent duplicate form submission
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = 'Please wait…';
      }
    });
  });

  // Highlight active link in sidebar navigation
  const adminSidebar = document.getElementById('admin-sidebar');
  const sidebarCollapseToggle = document.getElementById('sidebar-collapse-toggle');
  const sidebarToggle = document.getElementById('sidebar-toggle');
  const sidebarClose = document.getElementById('sidebar-close');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');

  const currentPath = window.location.pathname;
  const sidebarLinks = Array.from(document.querySelectorAll('.sidebar-nav a'));
  if (sidebarLinks.length > 0) {
    // Sort links by href length descending so specific paths take precedence
    sidebarLinks.sort((a, b) => (b.getAttribute('href') || '').length - (a.getAttribute('href') || '').length);

    let matched = false;
    for (const link of sidebarLinks) {
      const href = link.getAttribute('href');
      if (!href) continue;
      if (currentPath === href || (href !== '/' && href !== '/dashboard/' && currentPath.startsWith(href))) {
        link.classList.add('active');
        // Scroll active item smoothly into view within fixed sidebar if overflowing
        try {
          if (adminSidebar) {
            const sidebarRect = adminSidebar.getBoundingClientRect();
            const linkRect = link.getBoundingClientRect();
            if (linkRect.top < sidebarRect.top || linkRect.bottom > sidebarRect.bottom) {
              adminSidebar.scrollTop = link.offsetTop - (adminSidebar.clientHeight / 3);
            }
          }
        } catch (e) {}
        matched = true;
        break;
      }
    }
    if (!matched) {
      const defaultActive = document.querySelector('.sidebar-nav a[href="' + currentPath + '"]');
      if (defaultActive) defaultActive.classList.add('active');
    }
  }

  function updateToggleButtons(collapsed) {
    [sidebarCollapseToggle, sidebarToggle].forEach(btn => {
      if (btn) {
        btn.setAttribute('aria-expanded', String(!collapsed));
        btn.setAttribute('aria-label', collapsed ? 'Open sidebar navigation' : 'Collapse sidebar navigation');
        btn.title = collapsed ? 'Open sidebar navigation' : 'Collapse sidebar navigation';
      }
    });
  }

  function setSidebarCollapsed(collapsed) {
    // Enable transitions once user interacts
    document.documentElement.classList.add('sidebar-interactive');
    document.body.classList.add('sidebar-interactive');

    document.documentElement.classList.toggle('sidebar-collapsed', collapsed);
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    document.body.classList.toggle('sidebar-open', !collapsed);

    if (adminSidebar) {
      adminSidebar.classList.toggle('show', !collapsed);
    }
    if (sidebarBackdrop) {
      sidebarBackdrop.classList.toggle('show', !collapsed);
    }

    try {
      localStorage.setItem('admin-sidebar-collapsed', collapsed ? 'true' : 'false');
    } catch (e) {}

    updateToggleButtons(collapsed);
  }

  function toggleSidebar() {
    const isCurrentlyCollapsed = document.documentElement.classList.contains('sidebar-collapsed') ||
                                 document.body.classList.contains('sidebar-collapsed');
    setSidebarCollapsed(!isCurrentlyCollapsed);
  }

  // Sync initial state on load
  const isInitiallyCollapsed = document.documentElement.classList.contains('sidebar-collapsed');
  updateToggleButtons(isInitiallyCollapsed);

  if (sidebarCollapseToggle) sidebarCollapseToggle.addEventListener('click', toggleSidebar);
  if (sidebarToggle) sidebarToggle.addEventListener('click', toggleSidebar);
  if (sidebarClose) sidebarClose.addEventListener('click', () => setSidebarCollapsed(true));
  if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', () => setSidebarCollapsed(true));

  // Auto-collapse when clicking nav link on smaller screens
  document.querySelectorAll('.sidebar-nav a').forEach(link => {
    link.addEventListener('click', () => {
      if (window.innerWidth < 992) {
        setSidebarCollapsed(true);
      }
    });
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const isCollapsed = document.documentElement.classList.contains('sidebar-collapsed') ||
                          document.body.classList.contains('sidebar-collapsed');
      if (!isCollapsed && window.innerWidth < 992) {
        setSidebarCollapsed(true);
      }
    }
  });
});
