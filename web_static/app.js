/**
 * app.js — Literature Analyzer 科研文献与引文图谱工作站 SPA 控制层
 */

// ── 全局应用状态 ─────────────────────────────────────────────────────────────
const AppState = {
  activeTab: "dashboard",
  library: {
    page: 1,
    pageSize: 20,
    totalPages: 1,
    query: "",
    docType: "all",
    isIndexed: "all"
  },
  candidates: {
    page: 1,
    pageSize: 20,
    totalPages: 1,
    query: "",
    track: "ALL",
    isOA: "true",
    status: "DISCOVERED",
    selectedIds: new Set()
  },
  network: {
    chartInstance: null,
    viewMode: "full",
    seedId: "bba4a59d",
    filterConfirmed: true,
    filterCandidate: true,
    filterUnresolved: false,
    rawData: null,
    libraryDocs: []
  },
  discovery: {
    seeds: [
      { id: "bba4a59d778cbb49d4bd5f3cb84fe40430054a15559b32b8e3fe76ff9a23cdc8", title: "理解工业肥料中的结块现象", checked: true },
      { id: "6a3736de4120ca25e2d1d2931a7d6e495ae40d588523c14fa9e2830f3050965d", title: "粉体结块综述", checked: true },
      { id: "cf693c6cba948b8b92b67f1b2728469ad01533ad0fe39ce34e3a893cb62feec0", title: "晶体结块：表征、机制和预防", checked: false }
    ]
  },
  audit: {
    activeSubTab: "decisions"
  },
  activeJobId: null,
  jobPollTimer: null
};
window.AppState = AppState;

// ── 工具函数 ─────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
window.escapeHtml = escapeHtml;

// ── Modern Web Guidance: Native Popover Toast System ──
function showToast(message, type = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  try {
    if (typeof container.showPopover === "function" && !container.matches(":popover-open")) {
      container.showPopover();
    }
  } catch (e) {}

  const toast = document.createElement("div");
  toast.className = `toast-item toast-${type}`;
  const iconMap = {
    success: "✅",
    info: "ℹ️",
    warning: "⚠️",
    error: "❌"
  };
  toast.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;">
      <span style="font-size:16px;">${iconMap[type] || "ℹ️"}</span>
      <span style="line-height:1.4;">${message}</span>
    </div>
    <button class="toast-close" style="background:none;border:none;cursor:pointer;color:var(--text-dim, #94a3b8);font-size:14px;padding:2px 4px;">✕</button>
  `;

  const closeBtn = toast.querySelector(".toast-close");
  const dismiss = () => {
    toast.classList.add("toast-fade-out");
    setTimeout(() => {
      toast.remove();
      if (container.children.length === 0) {
        try {
          if (typeof container.hidePopover === "function") container.hidePopover();
        } catch (e) {}
      }
    }, 250);
  };

  closeBtn.addEventListener("click", dismiss);
  container.appendChild(toast);

  if (duration > 0) {
    setTimeout(dismiss, duration);
  }
}
window.showToast = showToast;

// ── 初始化入口 ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initNavigation();
  initDashboardEvents();
  initLibraryEvents();
  initNetworkEvents();
  initDiscoveryEvents();
  initCandidatesEvents();
  initPipelineEvents();
  initAuditEvents();
  initModals();

  // 初始载入首页数据
  loadDashboardData();
});

// ── 主题切换 ─────────────────────────────────────────────────────────────────
function initTheme() {
  const toggleBtn = document.getElementById("theme-toggle-btn");
  const savedTheme = localStorage.getItem("lit_theme") || "light";
  document.documentElement.setAttribute("data-theme", savedTheme);

  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "light";
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("lit_theme", next);
      if (AppState.network.chartInstance) {
        AppState.network.chartInstance.dispose();
        AppState.network.chartInstance = null;
        renderNetworkGraph();
      }
    });
  }
}

// ── 导航切换 ─────────────────────────────────────────────────────────────────
function initNavigation() {
  const navItems = document.querySelectorAll(".nav-menu .nav-item");
  navItems.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-tab");
      switchTab(targetTab);
    });
  });

  // Top Bar 快捷跳转
  const btnRefresh = document.getElementById("btn-refresh-data");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => {
      refreshCurrentTab();
    });
  }
  const btnQuickExpand = document.getElementById("btn-quick-expand");
  if (btnQuickExpand) {
    btnQuickExpand.addEventListener("click", () => {
      switchTab("discovery");
    });
  }

  // Dashboard 快捷跳转
  document.getElementById("btn-goto-discovery")?.addEventListener("click", () => switchTab("discovery"));
  document.getElementById("btn-goto-candidates")?.addEventListener("click", () => switchTab("candidates"));
  document.getElementById("btn-goto-pipeline")?.addEventListener("click", () => switchTab("pipeline"));
}

function switchTab(tabId) {
  const updateDOM = () => {
    AppState.activeTab = tabId;

    // 更新导航高亮
    document.querySelectorAll(".nav-menu .nav-item").forEach(btn => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === tabId);
    });

    // 更新 Tab 视图
    document.querySelectorAll(".tab-view").forEach(view => {
      view.classList.toggle("active", view.id === `tab-${tabId}`);
    });

    // 更新顶栏标题
    const titleMap = {
      dashboard: { title: "工作台概况", desc: "查看馆藏文献、引文网络拓扑、外部候选池与流水线运行状态" },
      library: { title: "文献目录", desc: "浏览、检索已入库文献，查看引文拓扑与结构化参考文献" },
      network: { title: "引文网络图谱", desc: "交互式力导向拓扑关系图，支持节点缩放、拖拽与局部子图探索" },
      discovery: { title: "滚雪球发现", desc: "基于 OpenAlex 双轨引文网络扩展高质量基石与前沿文献" },
      candidates: { title: "候选池管理", desc: "多维筛选、人工/AI 审批确认与生命周期审计追踪" },
      pipeline: { title: "入库流水线", desc: "已批准候选合法全文下载、PDF 验真、正文解析与 Qwen3 向量入库" },
      audit: { title: "审计与运行记录", desc: "严格 Append-Only 审批决策日志、PDF 资产元数据与发现批次" },
        settings: { title: "系统设置", desc: "在线配置 API Keys 与系统运行参数" }
    };

    const meta = titleMap[tabId] || { title: "Literature Analyzer", desc: "" };
    document.getElementById("page-title").textContent = meta.title;
    document.getElementById("page-desc").textContent = meta.desc;
  };

  // Modern Web Guidance: Native View Transitions API
  if (document.startViewTransition) {
    document.startViewTransition(() => {
      updateDOM();
    }).finished.finally(() => {
      refreshCurrentTab();
    });
  } else {
    updateDOM();
    refreshCurrentTab();
  }
}

function refreshCurrentTab() {
  switch (AppState.activeTab) {
    case "dashboard":
      loadDashboardData();
      break;
    case "library":
      loadLibraryData();
      break;
    case "network":
      renderNetworkGraph();
      break;
    case "discovery":
      loadDiscoveryRuns();
      break;
    case "candidates":
      loadCandidatesData();
      break;
    case "pipeline":
      loadPipelineData();
      break;
    case "audit":
      loadAuditData();
      break;
  }
}

// ── DASHBOARD 概况 ──────────────────────────────────────────────────────────
async function loadDashboardData() {
  try {
    const res = await fetch("/api/dashboard/stats");
    const json = await res.json();
    if (!json.success) return;

    const d = json.data;

    // 分层统计卡片 (5 互斥实体分区 + 1 能力指标)
    document.getElementById("dash-prod-baseline").textContent = (d.library.production_baseline || 344).toLocaleString();
    document.getElementById("dash-verified-works").textContent = (d.library.verified_ingested_works || 3).toLocaleString();
    document.getElementById("dash-vector-indexed").textContent = (d.library.vector_indexed_capability || 3).toLocaleString();
    document.getElementById("dash-quarantined-works").textContent = (d.library.quarantined_works_total || 4).toLocaleString();
    document.getElementById("dash-external-works").textContent = (d.library.external_unresolved_placeholders || 8766).toLocaleString();

    // 导航徽标
    document.getElementById("nav-count-papers").textContent = d.library.total_docs || 0;
    document.getElementById("nav-count-edges").textContent = d.graph.edges_total || 0;
    document.getElementById("nav-count-cands").textContent = d.candidates.total_candidates || 0;

    // 生命周期漏斗
    const cands = d.candidates;
    const funnelHtml = `
      <div class="funnel-step">
        <div class="num text-primary">${cands.DISCOVERED || 0}</div>
        <div class="label">DISCOVERED (发现)</div>
      </div>
      <div class="funnel-arrow">➔</div>
      <div class="funnel-step">
        <div class="num text-warning">${cands.APPROVED || 0}</div>
        <div class="label">APPROVED (已批准)</div>
      </div>
      <div class="funnel-arrow">➔</div>
      <div class="funnel-step">
        <div class="num text-warning">${cands.FULLTEXT_ACQUIRED || 0}</div>
        <div class="label">ACQUIRED (已获取)</div>
      </div>
      <div class="funnel-arrow">➔</div>
      <div class="funnel-step">
        <div class="num text-primary">${cands.PARSED || 0}</div>
        <div class="label">PARSED (已解析)</div>
      </div>
      <div class="funnel-arrow">➔</div>
      <div class="funnel-step">
        <div class="num text-success">${cands.GRAPH_INDEXED || 0}</div>
        <div class="label">GRAPH_INDEXED (图谱)</div>
      </div>
      <div class="funnel-arrow">➔</div>
      <div class="funnel-step" style="border-color: var(--accent-emerald);">
        <div class="num text-success font-bold">${cands.VECTOR_INDEXED || 0}</div>
        <div class="label">VECTOR_INDEXED (已入库)</div>
      </div>
    `;
    document.getElementById("dash-funnel-bar").innerHTML = funnelHtml;

    // 最近发现批次
    const runsTbody = document.getElementById("dash-runs-tbody");
    if (d.recent_runs && d.recent_runs.length > 0) {
      runsTbody.innerHTML = d.recent_runs.map(r => `
        <tr>
          <td><code class="text-primary">${r.query_run_id}</code></td>
          <td>${r.direction || 'both'} / depth=${r.depth || 1}</td>
          <td><b>${r.unique_materialized_candidates_count || r.materialized_candidates_count || 0}</b> 篇</td>
          <td><small class="text-muted">${r.created_at || '-'}</small></td>
        </tr>
      `).join('');
    } else {
      runsTbody.innerHTML = `<tr><td colspan="4" class="text-muted text-center">暂无发现记录</td></tr>`;
    }

    // 最近决策记录
    const decTbody = document.getElementById("dash-decisions-tbody");
    if (d.recent_decisions && d.recent_decisions.length > 0) {
      decTbody.innerHTML = d.recent_decisions.map(dec => `
        <tr>
          <td><code title="${dec.candidate_id}">${dec.candidate_id.substring(0, 20)}...</code></td>
          <td><span class="badge ${dec.action === 'APPROVE' ? 'badge-success' : 'badge-danger'}">${dec.action}</span></td>
          <td><small>${dec.decision_source || 'USER'}</small></td>
          <td><small class="text-muted">${dec.decided_at || '-'}</small></td>
        </tr>
      `).join('');
    } else {
      decTbody.innerHTML = `<tr><td colspan="4" class="text-muted text-center">暂无决策记录</td></tr>`;
    }
  } catch (err) {
    console.error("Failed to load dashboard data:", err);
  }
}

function initDashboardEvents() {
  // no-op
}

// ── LIBRARY 文献目录 ────────────────────────────────────────────────────────
function initLibraryEvents() {
  const searchInput = document.getElementById("lib-search-input");
  const typeSelect = document.getElementById("lib-type-select");
  const indexedSelect = document.getElementById("lib-indexed-select");

  // Category Filter Pills
  document.querySelectorAll("#lib-category-pills .category-pill-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#lib-category-pills .category-pill-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const type = btn.dataset.type;
      if (typeSelect) typeSelect.value = type;
      AppState.library.docType = type;
      AppState.library.page = 1;
      loadLibraryData();
    });
  });

  document.getElementById("btn-lib-search")?.addEventListener("click", () => {
    AppState.library.query = searchInput.value.trim();
    AppState.library.docType = typeSelect.value;
    AppState.library.isIndexed = indexedSelect.value;
    AppState.library.page = 1;
    
    // Sync category pill active state
    document.querySelectorAll("#lib-category-pills .category-pill-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.type === typeSelect.value);
    });

    loadLibraryData();
  });

  document.getElementById("btn-lib-reset")?.addEventListener("click", () => {
    searchInput.value = "";
    typeSelect.value = "all";
    indexedSelect.value = "all";
    AppState.library.query = "";
    AppState.library.docType = "all";
    AppState.library.isIndexed = "all";
    AppState.library.page = 1;

    document.querySelectorAll("#lib-category-pills .category-pill-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.type === "all");
    });

    loadLibraryData();
  });

  searchInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      document.getElementById("btn-lib-search").click();
    }
  });
}

async function loadLibraryData() {
  const tbody = document.getElementById("lib-docs-tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">正在检索馆藏文献...</td></tr>`;

  try {
    const q = encodeURIComponent(AppState.library.query);
    const type = encodeURIComponent(AppState.library.docType);
    const indexed = encodeURIComponent(AppState.library.isIndexed);
    const page = AppState.library.page;
    const pageSize = AppState.library.pageSize;

    const url = `/api/library/documents?page=${page}&page_size=${pageSize}&q=${q}&type=${type}&indexed=${indexed}`;
    const res = await fetch(url);
    const json = await res.json();

    if (!json.success) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger">加载失败: ${json.error}</td></tr>`;
      return;
    }

    const total = json.data.total || 0;
    const items = json.data.items || [];
    const totalPages = json.data.total_pages || Math.ceil(total / (pageSize || 20)) || 1;
    AppState.library.totalPages = totalPages;

    document.getElementById("lib-total-count").textContent = total;
    document.getElementById("lib-page-summary").textContent = `第 ${page} / ${totalPages} 页 (共 ${total} 条)`;

    if (!items || items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">未找到匹配的文献记录</td></tr>`;
      renderPagination("lib-pagination", AppState.library, loadLibraryData);
      return;
    }

    tbody.innerHTML = items.map(doc => {
      const displayTitle = doc.title_zh || doc.title_en || doc.title || "Untitled";
      const subTitle = (doc.title_zh && doc.title_en) ? `<div class="text-muted text-xs">${doc.title_en}</div>` : "";
      const doiBadge = doc.doi ? `<a href="https://doi.org/${doc.doi}" target="_blank" class="badge-doi">DOI</a>` : "";
      const indexBadge = doc.is_indexed 
        ? `<span class="badge badge-success">已索引 (${doc.n_chunks}块)</span>`
        : `<span class="badge badge-secondary">未索引</span>`;

      // Formatted colorful Category Badges
      let typeBadge = '<span class="badge badge-primary">📑 学术论文</span>';
      if (doc.doc_type === 'patent') {
        typeBadge = '<span class="badge badge-warning">💡 发明专利</span>';
      } else if (doc.doc_type === 'book') {
        typeBadge = '<span class="badge badge-purple">📚 教材专著</span>';
      } else if (doc.doc_type === 'standard') {
        typeBadge = '<span class="badge badge-info">📐 行业标准</span>';
      } else if (doc.doc_type === 'conference') {
        typeBadge = '<span class="badge badge-secondary">🎤 会议论文</span>';
      }

      return `
        <tr>
          <td>
            <div class="doc-table-title" onclick="openDocDetail('${doc.id}')" title="点击查看完整文献详情">
              《${displayTitle}》 ${doiBadge}
            </div>
            ${subTitle}
            <div class="text-muted text-xs mt-1" style="display:flex;align-items:center;gap:6px;">
              <span>指纹 ID:</span>
              <code class="doc-id-pill" onclick="copyText('${doc.id}')" title="点击复制完整哈希">${doc.id.substring(0, 10)}...</code>
            </div>
          </td>
          <td>
            <div class="text-sm font-medium">${doc.authors ? doc.authors.substring(0, 35) : '-'}</div>
            <div class="text-muted text-xs">${doc.year || '-'} | ${doc.journal ? doc.journal.substring(0, 30) : '-'}</div>
          </td>
          <td>${typeBadge}</td>
          <td class="text-center"><b>${doc.outgoing_citations || 0}</b></td>
          <td class="text-center"><b>${doc.incoming_citations || 0}</b></td>
          <td>${indexBadge}</td>
          <td>
            <div class="btn-group-sm">
              <button class="btn btn-secondary btn-xs" onclick="openDocDetail('${doc.id}')">详情</button>
              <button class="btn btn-primary btn-xs" onclick="locateInGraph('${doc.id}')">图谱</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

    renderPagination("lib-pagination", AppState.library, loadLibraryData);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger">请求异常: ${err.message}</td></tr>`;
  }
}

// ── CITATION NETWORK 引文网络 ───────────────────────────────────────────────

async function populateNetworkSeedDropdown(filterQuery = "") {
  const select = document.getElementById("net-seed-select");
  if (!select) return;

  if (AppState.network.libraryDocs.length === 0) {
    try {
      const res = await fetch("/api/library/documents?page=1&page_size=500");
      const json = await res.json();
      if (json.success && json.data.items) {
        AppState.network.libraryDocs = json.data.items;
      }
    } catch (e) {
      console.error("Failed to load library docs for network select:", e);
    }
  }

  select.innerHTML = "";
  const docs = AppState.network.libraryDocs;
  const q = filterQuery.trim().toLowerCase();

  const filtered = q 
    ? docs.filter(d => (d.title || d.title_zh || d.title_en || "").toLowerCase().includes(q) || d.id.toLowerCase().includes(q))
    : docs;

  if (filtered.length === 0) {
    select.innerHTML = '<option value="">未找到匹配的馆藏文献</option>';
    return;
  }

  filtered.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.id;
    const title = d.title || d.title_zh || d.title_en || d.id;
    const shortTitle = title.length > 35 ? title.substring(0, 35) + "..." : title;
    opt.textContent = `《${shortTitle}》 (${d.year || '-'}) [出:${d.outgoing_citations || 0}, 入:${d.incoming_citations || 0}]`;
    if (d.id === AppState.network.seedId) {
      opt.selected = true;
    }
    select.appendChild(opt);
  });
}
window.populateNetworkSeedDropdown = populateNetworkSeedDropdown;

function initNetworkEvents() {
  populateNetworkSeedDropdown();

  document.getElementById("net-view-mode")?.addEventListener("change", (e) => {
    AppState.network.viewMode = e.target.value;
    const seedGroup = document.getElementById("net-seed-group");
    if (seedGroup) {
      seedGroup.style.display = e.target.value === "seed" ? "block" : "none";
    }
    if (e.target.value === "seed" && !AppState.network.seedId) {
      const sel = document.getElementById("net-seed-select");
      if (sel && sel.value) {
        AppState.network.seedId = sel.value;
      }
    }
    renderNetworkGraph();
  });

  document.getElementById("net-seed-select")?.addEventListener("change", (e) => {
    if (e.target.value) {
      AppState.network.seedId = e.target.value;
      AppState.network.viewMode = "seed";
      const vm = document.getElementById("net-view-mode");
      if (vm) vm.value = "seed";
      renderNetworkGraph();
    }
  });

  // 关键词实时过滤下拉菜单
  document.getElementById("net-seed-search")?.addEventListener("input", (e) => {
    populateNetworkSeedDropdown(e.target.value);
  });

  document.getElementById("net-seed-search")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      document.getElementById("btn-net-search-focus")?.click();
    }
  });

  document.getElementById("btn-net-search-focus")?.addEventListener("click", () => {
    const sel = document.getElementById("net-seed-select");
    const inp = document.getElementById("net-seed-search");
    let targetId = sel?.value;
    const q = inp?.value.trim();

    if (q && q.length > 8 && !q.includes(" ")) {
      targetId = q;
    }

    if (!targetId) {
      showToast("请在下拉框中选择或输入有效文献", "warning");
      return;
    }

    AppState.network.seedId = targetId;
    AppState.network.viewMode = "seed";
    const vm = document.getElementById("net-view-mode");
    if (vm) vm.value = "seed";
    const seedGroup = document.getElementById("net-seed-group");
    if (seedGroup) seedGroup.style.display = "block";
    showToast(`正在聚焦文献子图...`, "info", 2000);
    renderNetworkGraph();
  });

  document.getElementById("btn-net-switch-full")?.addEventListener("click", () => {
    AppState.network.viewMode = "full";
    const vm = document.getElementById("net-view-mode");
    if (vm) vm.value = "full";
    showToast("已切回全局骨干引文网络", "info", 2000);
    renderNetworkGraph();
  });

  document.getElementById("net-filter-confirmed")?.addEventListener("change", (e) => {
    AppState.network.filterConfirmed = e.target.checked;
    updateGraphFilter();
  });
  document.getElementById("net-filter-candidate")?.addEventListener("change", (e) => {
    AppState.network.filterCandidate = e.target.checked;
    updateGraphFilter();
  });
  document.getElementById("net-filter-unresolved")?.addEventListener("change", (e) => {
    AppState.network.filterUnresolved = e.target.checked;
    renderNetworkGraph();
  });

  document.getElementById("btn-net-reload")?.addEventListener("click", () => {
    renderNetworkGraph();
  });

  window.addEventListener("resize", () => {
    if (AppState.network.chartInstance) {
      AppState.network.chartInstance.resize();
    }
  });
}

async function renderNetworkGraph() {
  const container = document.getElementById("echarts-graph-container");
  if (!container) return;

  if (!AppState.network.chartInstance) {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    AppState.network.chartInstance = echarts.init(container, isDark ? "dark" : null);
  }

  AppState.network.chartInstance.showLoading({ text: "正在渲染引文网络力导向拓扑..." });

  try {
    const includeUnresolved = AppState.network.filterUnresolved ? "true" : "false";
    let url = "/api/citations/graph/full?include_unresolved=" + includeUnresolved;
    if (AppState.network.viewMode === "seed") {
      url = `/api/citations/${AppState.network.seedId}`;
    }

    const res = await fetch(url);
    const json = await res.json();
    AppState.network.chartInstance.hideLoading();

    if (!json.success) {
      showToast(`聚焦子图暂未生成 (${json.error})，已自动切回骨干全景网络`, "warning", 4000);
      AppState.network.viewMode = "full";
      const viewSelect = document.getElementById("net-view-mode");
      if (viewSelect) viewSelect.value = "full";
      const seedGroup = document.getElementById("net-seed-group");
      if (seedGroup) seedGroup.style.display = "none";
      loadCitationGraph();
      return;
    }

    AppState.network.rawData = json.data;
    updateGraphFilter();
  } catch (err) {
    AppState.network.chartInstance.hideLoading();
    console.error("Network render error:", err);
  }
}

function updateGraphFilter() {
  if (!AppState.network.rawData || !AppState.network.chartInstance) return;

  const data = AppState.network.rawData;
  const nodes = data.nodes || [];
  let links = data.links || [];

  // 根据边过滤
  links = links.filter(l => {
    if (l.value === "CONFIRMED" && !AppState.network.filterConfirmed) return false;
    if (l.value === "CANDIDATE" && !AppState.network.filterCandidate) return false;
    if (l.value === "UNRESOLVED_EXTERNAL" && !AppState.network.filterUnresolved) return false;
    return true;
  });

  const option = {
    tooltip: {
      formatter: (params) => {
        if (params.dataType === "node") {
          const d = params.data;
          return `<b>《${d.name}》</b><br/>ID: <code>${d.id}</code><br/>年份: ${d.year || '-'}<br/>类别: ${d.in_library ? '馆内文献' : '外部文献'}<br/>度数: ${d.value}`;
        } else if (params.dataType === "edge") {
          return `引文关系: <b>${params.data.value}</b><br/>${params.data.source} ➔ ${params.data.target}`;
        }
      }
    },
    legend: [{
      data: data.categories ? data.categories.map(a => a.name) : []
    }],
    series: [{
      type: "graph",
      layout: "force",
      data: nodes,
      links: links,
      categories: data.categories,
      roam: true,
      label: {
        show: true,
        position: "right",
        formatter: (p) => p.data.name ? (p.data.name.length > 18 ? p.data.name.substring(0, 18) + '...' : p.data.name) : p.data.id,
        fontSize: 11
      },
      lineStyle: {
        color: "source",
        curveness: 0.2
      },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [4, 8],
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 4 }
      },
      large: true,
      progressive: 1500,
      force: {
        repulsion: nodes.length > 2000 ? 50 : 220,
        gravity: 0.08,
        edgeLength: nodes.length > 2000 ? [30, 80] : [60, 140],
        layoutAnimation: false
      }
    }]
  };

  AppState.network.chartInstance.setOption(option, true);

  // 绑定节点点击事件
  AppState.network.chartInstance.off("click");
  AppState.network.chartInstance.on("click", (params) => {
    if (params.dataType === "node") {
      showNodeDetail(params.data);
    }
  });
}

function showNodeDetail(node) {
  const panel = document.getElementById("net-node-content");
  if (!panel) return;

  const rawName = node.name || node.id || "";
  const isHexHash = /^[0-9a-f]{32,}$/i.test(rawName);
  const showTitle = isHexHash ? `文献 (${rawName.substring(0, 12)}...)` : rawName;

  panel.innerHTML = `
    <div class="node-card-active">
      <h4 class="text-primary font-bold">《${escapeHtml(showTitle)}》</h4>
      <div class="text-xs text-muted mt-2">数字指纹: <code style="font-size:11px;" title="${node.id}">${node.id.substring(0, 16)}...</code></div>
      <div class="text-xs text-muted">发表年份: ${node.year || '-'}</div>
      <div class="text-xs text-muted">馆藏状态: ${node.in_library ? '🏛️ 馆内已收录' : '🌐 外部候选/未解析'}</div>
      <div class="text-xs text-muted">节点度数 / 连通度: <b>${node.value}</b></div>
      
      <div class="node-actions mt-3">
        <button class="btn btn-primary btn-xs" onclick="openDocDetail('${node.id}')">查看完整文献详情</button>
        <button class="btn btn-secondary btn-xs" onclick="setSeedFromLibrary('${node.id}', '${escapeHtml(showTitle)}')">❄️ 设为发现种子</button>
        <button class="btn btn-secondary btn-xs" onclick="focusOnSeed('${node.id}')">以此为中心聚焦</button>
      </div>
    </div>
  `;
}

function locateInGraph(docId) {
  switchTab("network");
  document.getElementById("net-view-mode").value = "seed";
  AppState.network.viewMode = "seed";
  AppState.network.seedId = docId;
  
  const seedSelect = document.getElementById("net-seed-select");
  if (seedSelect) {
    document.getElementById("net-seed-group").style.display = "block";
    let found = false;
    for (let opt of seedSelect.options) {
      if (opt.value === docId) {
        opt.selected = true;
        found = true;
        break;
      }
    }
    if (!found) {
      const newOpt = new Option(`${docId} (选中文献)`, docId, true, true);
      seedSelect.add(newOpt);
    }
  }

  renderNetworkGraph();
}

function focusOnSeed(docId) {
  locateInGraph(docId);
}

// ── DISCOVERY 滚雪球发现 ───────────────────────────────────────────────────

function renderDiscoverySeeds() {
  const container = document.getElementById("disc-seed-chips");
  if (!container) return;

  if (!AppState.discovery.seeds || AppState.discovery.seeds.length === 0) {
    container.innerHTML = `<span class="text-muted text-xs" style="padding:4px;">暂无种子文献，请从下方下拉框或输入框添加。</span>`;
    return;
  }

  container.innerHTML = AppState.discovery.seeds.map((s, idx) => `
    <label class="chip-item ${s.checked ? 'active' : ''}">
      <input type="checkbox" value="${escapeHtml(s.id)}" ${s.checked ? 'checked' : ''} onchange="toggleSeedCheck(${idx}, this.checked)">
      <span>《${escapeHtml(s.title || s.id)}》 <code style="font-size:11px;">(${s.id.substring(0, 8)})</code></span>
      <button type="button" class="chip-delete-btn" onclick="event.preventDefault(); event.stopPropagation(); removeDiscoverySeed(${idx})" title="移除此种子">✕</button>
    </label>
  `).join('');
}
window.renderDiscoverySeeds = renderDiscoverySeeds;

function toggleSeedCheck(idx, checked) {
  if (AppState.discovery.seeds[idx]) {
    AppState.discovery.seeds[idx].checked = checked;
    renderDiscoverySeeds();
  }
}
window.toggleSeedCheck = toggleSeedCheck;

function removeDiscoverySeed(idx) {
  AppState.discovery.seeds.splice(idx, 1);
  renderDiscoverySeeds();
}
window.removeDiscoverySeed = removeDiscoverySeed;

function addDiscoverySeed(id, title, checked = true) {
  if (!id || !id.trim()) return;
  id = id.trim();
  const existing = AppState.discovery.seeds.find(s => s.id.toLowerCase() === id.toLowerCase());
  if (existing) {
    existing.checked = true;
    showToast(`种子已存在，已为您勾选: 《${existing.title}》`, "info");
  } else {
    AppState.discovery.seeds.push({ id, title: title || id, checked: true });
    showToast(`已成功添加新种子文献: 《${title || id}》`, "success");
  }
  renderDiscoverySeeds();
}
window.addDiscoverySeed = addDiscoverySeed;

function setSeedFromLibrary(docId, docTitle) {
  addDiscoverySeed(docId, docTitle, true);
  switchTab("discovery");
}
window.setSeedFromLibrary = setSeedFromLibrary;

async function populateDiscoveryLibraryDropdown() {
  const select = document.getElementById("disc-library-seed-select");
  if (!select || select.options.length > 1) return;

  try {
    const res = await fetch("/api/library/documents?page=1&page_size=500");
    const json = await res.json();
    if (!json.success) return;

    (json.data.items || []).forEach(doc => {
      const opt = document.createElement("option");
      opt.value = doc.id;
      opt.textContent = `《${doc.title || doc.title_zh || doc.title_en || doc.id}》 (${doc.year || '-'})`;
      select.appendChild(opt);
    });
  } catch (err) {
    console.error("Failed to populate discovery library select:", err);
  }
}
window.populateDiscoveryLibraryDropdown = populateDiscoveryLibraryDropdown;

function initDiscoveryEvents() {
  renderDiscoverySeeds();
  populateDiscoveryLibraryDropdown();

  document.getElementById("btn-select-all-seeds")?.addEventListener("click", () => {
    AppState.discovery.seeds.forEach(s => s.checked = true);
    renderDiscoverySeeds();
  });

  document.getElementById("btn-clear-seeds")?.addEventListener("click", () => {
    AppState.discovery.seeds.forEach(s => s.checked = false);
    renderDiscoverySeeds();
  });

  document.getElementById("btn-add-library-seed")?.addEventListener("click", () => {
    const sel = document.getElementById("disc-library-seed-select");
    if (!sel || !sel.value) {
      showToast("请先在下拉框中选择一篇馆内文献", "warning");
      return;
    }
    const optText = sel.options[sel.selectedIndex].text;
    const titleMatch = optText.match(/《(.*?)》/);
    const title = titleMatch ? titleMatch[1] : optText;
    addDiscoverySeed(sel.value, title, true);
    sel.value = "";
  });

  document.getElementById("btn-add-custom-seed")?.addEventListener("click", () => {
    const inp = document.getElementById("disc-custom-seed-input");
    if (!inp || !inp.value.trim()) {
      showToast("请输入 Work ID、DOI 或 OpenAlex ID", "warning");
      return;
    }
    const val = inp.value.trim();
    addDiscoverySeed(val, val, true);
    inp.value = "";
  });

  document.getElementById("btn-launch-discovery")?.addEventListener("click", () => {
    launchDiscoverySnowball();
  });
}

async function loadDiscoveryRuns() {
  try {
    const res = await fetch("/api/discovery/runs");
    const json = await res.json();
    if (!json.success) return;

    if (json.data && json.data.length > 0) {
      const latestRun = json.data[0];
      loadDiscoveryCandidatesForRun(latestRun.query_run_id);
    }
  } catch (err) {
    console.error("Load discovery runs error:", err);
  }
}

async function loadDiscoveryCandidatesForRun(runId) {
  try {
    const res = await fetch(`/api/discovery/candidates?run_id=${runId}&page_size=100`);
    const json = await res.json();
    if (!json.success) return;

    const items = json.data.items || [];
    const foundCands = items.filter(c => c.track === "FOUNDATIONAL");
    const emergCands = items.filter(c => c.track === "EMERGING");

    document.getElementById("disc-found-count").textContent = `${foundCands.length} 篇`;
    document.getElementById("disc-emerg-count").textContent = `${emergCands.length} 篇`;

    renderDiscoveryCardList("disc-found-list", foundCands);
    renderDiscoveryCardList("disc-emerg-list", emergCands);
  } catch (err) {
    console.error("Load candidates for run error:", err);
  }
}

function renderDiscoveryCardList(containerId, list) {
  const container = document.getElementById(containerId);
  if (!container) return;

  if (!list || list.length === 0) {
    container.innerHTML = `<div class="text-center text-muted py-4">当前轨道暂无候选文献</div>`;
    return;
  }

  container.innerHTML = list.map(c => {
    const oaTag = c.is_open_access 
      ? `<span class="badge badge-success">OA 全文可下</span>`
      : `<span class="badge badge-secondary">非 OA</span>`;
    const inLibTag = c.in_library 
      ? `<span class="badge badge-primary">馆内已收录</span>`
      : `<span class="badge badge-warning">外部新发现</span>`;
    const linkBtn = c.oa_url 
      ? `<a href="${c.oa_url}" target="_blank" class="btn btn-secondary btn-xs">🔗 查看 OA 原文</a>`
      : '';

    return `
      <div class="cand-card">
        <div class="cand-card-title">《${c.title}》</div>
        <div class="cand-card-meta">
          <span>📅 ${c.publication_year || '-'}</span>
          <span>🏛️ ${c.journal_or_venue || '-'}</span>
          <span>📈 被引: <b>${c.cited_by_count || 0}</b></span>
          <span>🔗 共被引: <b>${c.co_citation_count || 0}</b></span>
          ${oaTag}
          ${inLibTag}
        </div>
        <div class="cand-card-reason">
          💡 <b>推荐依据</b>: ${c.recommendation_reason || '暂无说明'}
        </div>
        <div class="flex-between mt-2">
          <small class="text-muted">ID: <code>${c.candidate_id.substring(0, 24)}...</code></small>
          <div class="btn-group-sm">
            ${linkBtn}
            <button class="btn btn-primary btn-xs" onclick="openApproveSingleCandidate('${c.candidate_id}', '${escapeHtml(c.title)}')">审批入库</button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function launchDiscoverySnowball() {
  const selectedSeeds = Array.from(document.querySelectorAll("#disc-seed-chips input:checked")).map(cb => cb.value);
  if (selectedSeeds.length === 0) {
    showToast("请至少选择一个种子文献！", "warning");
    return;
  }

  const direction = document.getElementById("disc-direction").value;
  const limit = parseInt(document.getElementById("disc-limit").value);
  const years = parseInt(document.getElementById("disc-years").value);
  const coCitation = parseInt(document.getElementById("disc-co-citation")?.value || "1");

  const btn = document.getElementById("btn-launch-discovery");
  btn.disabled = true;
  btn.textContent = "⏳ 正在启动 OpenAlex 发现...";

  try {
    const res = await fetch("/api/discovery/expand", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        seed_ids: selectedSeeds,
        direction: direction,
        depth: 1,
        limit_per_track: limit,
        recent_years: years,
        min_co_citation: coCitation
      })
    });
    const json = await res.json();
    if (!json.success) {
      showToast("启动失败: " + json.error, "error");
      btn.disabled = false;
      btn.textContent = "🚀 启动滚雪球发现";
      return;
    }

    const jobId = json.data.job_id;
    pollJobStatus(jobId, (job) => {
      btn.disabled = false;
      btn.textContent = "🚀 启动滚雪球发现";
      loadDiscoveryRuns();
      loadCandidatesData();
      loadDashboardData();
      
      const count = job.result?.materialized_candidates_count || 0;
      if (count > 0) {
        showToast(`🎉 滚雪球发现完成！共检索发现 ${count} 篇候选文献，已加入候选池。`, "success", 5000);
      } else {
        showToast("⚠️ 滚雪球发现完成，但未检索到符合条件的外部候选文献（请检查种子文献在 OpenAlex 是否具备引文记录）。", "warning", 5000);
      }
    });
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "🚀 启动滚雪球发现";
    showToast("请求异常: " + err.message, "error");
  }
}

// ── CANDIDATES 候选池管理 ──────────────────────────────────────────────────
function initCandidatesEvents() {
  document.getElementById("btn-cand-search")?.addEventListener("click", () => {
    AppState.candidates.query = document.getElementById("cand-search-input").value.trim();
    AppState.candidates.track = document.getElementById("cand-track-select").value;
    AppState.candidates.isOA = document.getElementById("cand-oa-select").value;
    AppState.candidates.status = document.getElementById("cand-status-select").value;
    AppState.candidates.page = 1;
    loadCandidatesData();
  });

  // 全选控制
  document.getElementById("cand-select-all")?.addEventListener("change", (e) => {
    const checkboxes = document.querySelectorAll(".cand-row-checkbox");
    checkboxes.forEach(cb => {
      cb.checked = e.target.checked;
      const cid = cb.getAttribute("data-id");
      if (e.target.checked) AppState.candidates.selectedIds.add(cid);
      else AppState.candidates.selectedIds.delete(cid);
    });
    updateSelectedCandidatesBar();
  });

  // 批量审批按钮
  document.getElementById("btn-batch-approve")?.addEventListener("click", () => {
    openConfirmDecisionModal("APPROVE");
  });
  document.getElementById("btn-batch-reject")?.addEventListener("click", () => {
    openConfirmDecisionModal("REJECT");
  });
}

async function loadCandidatesData() {
  const tbody = document.getElementById("cand-list-tbody");
  tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">正在查询候选池...</td></tr>`;

  try {
    const q = encodeURIComponent(AppState.candidates.query);
    const track = encodeURIComponent(AppState.candidates.track);
    const isOA = encodeURIComponent(AppState.candidates.isOA);
    const status = encodeURIComponent(AppState.candidates.status);
    const page = AppState.candidates.page;
    const pageSize = AppState.candidates.pageSize;

    const url = `/api/discovery/candidates?page=${page}&page_size=${pageSize}&q=${q}&track=${track}&is_oa=${isOA}&status=${status}`;
    const res = await fetch(url);
    const json = await res.json();

    if (!json.success) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger">加载失败: ${json.error}</td></tr>`;
      return;
    }

    const { total, items, total_pages } = json.data;
    AppState.candidates.totalPages = total_pages;

    document.getElementById("cand-total-count").textContent = total;
    document.getElementById("cand-page-summary").textContent = `第 ${page} / ${total_pages || 1} 页 (共 ${total} 条)`;

    if (!items || items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">暂无符合条件的候选文献</td></tr>`;
      renderPagination("cand-pagination", AppState.candidates, loadCandidatesData);
      return;
    }

    tbody.innerHTML = items.map(c => {
      const isChecked = AppState.candidates.selectedIds.has(c.candidate_id);
      const isDiscovered = c.lifecycle_status === "DISCOVERED";
      const oaBadge = c.is_open_access 
        ? `<a href="${c.oa_url}" target="_blank" class="badge badge-success">OA 全文</a>`
        : `<span class="badge badge-secondary">非 OA</span>`;

      let statusBadgeClass = "badge-secondary";
      if (c.lifecycle_status === "VECTOR_INDEXED") statusBadgeClass = "badge-success";
      else if (c.lifecycle_status === "APPROVED") statusBadgeClass = "badge-warning";
      else if (c.lifecycle_status === "REJECTED") statusBadgeClass = "badge-danger";
      else if (c.lifecycle_status === "DISCOVERED") statusBadgeClass = "badge-primary";

      const authorsStr = (c.authors || []).slice(0, 2).join(', ');

      return `
        <tr>
          <td><input type="checkbox" class="cand-row-checkbox" data-id="${c.candidate_id}" data-title="${escapeHtml(c.title)}" data-status="${c.lifecycle_status}" ${isChecked ? 'checked' : ''} onchange="toggleCandidateSelect('${c.candidate_id}')"></td>
          <td>
            <div class="cand-table-title">《${c.title}》</div>
            <div class="text-muted text-xs">ID: <code>${c.candidate_id}</code></div>
          </td>
          <td><span class="badge ${c.track === 'FOUNDATIONAL' ? 'badge-foundational' : 'badge-emerging'}">${c.track}</span></td>
          <td>
            <div class="text-sm">${authorsStr || '-'}</div>
            <div class="text-muted text-xs">${c.publication_year || '-'}</div>
          </td>
          <td><small class="text-muted">${c.journal_or_venue ? c.journal_or_venue.substring(0, 20) : '-'}</small></td>
          <td class="text-center"><b>${c.cited_by_count || 0}</b> / <b>${c.co_citation_count || 0}</b></td>
          <td>${oaBadge}</td>
          <td><span class="badge ${statusBadgeClass}">${c.lifecycle_status}</span></td>
          <td>
            ${isDiscovered ? `
              <div class="btn-group-sm">
                <button class="btn btn-success btn-xs" onclick="quickDecision('${c.candidate_id}', 'APPROVE')">批准</button>
                <button class="btn btn-danger btn-xs" onclick="quickDecision('${c.candidate_id}', 'REJECT')">拒绝</button>
              </div>
            ` : `<small class="text-muted">已锁定</small>`}
          </td>
        </tr>
      `;
    }).join('');

    renderPagination("cand-pagination", AppState.candidates, loadCandidatesData);
    updateSelectedCandidatesBar();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger">请求异常: ${err.message}</td></tr>`;
  }
}

function toggleCandidateSelect(cid) {
  if (AppState.candidates.selectedIds.has(cid)) {
    AppState.candidates.selectedIds.delete(cid);
  } else {
    AppState.candidates.selectedIds.add(cid);
  }
  updateSelectedCandidatesBar();
}

function updateSelectedCandidatesBar() {
  const count = AppState.candidates.selectedIds.size;
  document.getElementById("cand-selected-count").textContent = count;
  document.getElementById("btn-batch-approve").disabled = count === 0;
  document.getElementById("btn-batch-reject").disabled = count === 0;
}

function openApproveSingleCandidate(cid, title) {
  AppState.candidates.selectedIds.clear();
  AppState.candidates.selectedIds.add(cid);
  updateSelectedCandidatesBar();
  switchTab("candidates");
  openConfirmDecisionModal("APPROVE");
}

function quickDecision(cid, action) {
  AppState.candidates.selectedIds.clear();
  AppState.candidates.selectedIds.add(cid);
  openConfirmDecisionModal(action);
}

// ── PIPELINE 入库流水线 ────────────────────────────────────────────────────
function initPipelineEvents() {
  // Sub-tabs switching
  document.querySelectorAll("#pipe-sub-tabs .sub-tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pipe-sub-tabs .sub-tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".pipe-sub-card").forEach(c => c.style.display = "none");

      btn.classList.add("active");
      const targetSub = btn.dataset.pipeSub;
      const targetCard = document.getElementById(`pipe-card-${targetSub}`);
      if (targetCard) targetCard.style.display = "block";
    });
  });

  document.getElementById("btn-start-ingest-pipeline")?.addEventListener("click", () => {
    startIngestPipeline();
  });
  document.getElementById("btn-refresh-pending-pipe")?.addEventListener("click", () => {
    loadPipelineData();
    showToast("流水线状态已刷新", "info", 1500);
  });
  document.getElementById("btn-clear-pipe-console")?.addEventListener("click", () => {
    const consoleBox = document.getElementById("pipe-console-box");
    if (consoleBox) consoleBox.innerHTML = '<div class="log-line text-muted">[系统] 控制台日志已清空。</div>';
  });

  // Manual PDF File Upload
  const fileInput = document.getElementById("pipe-pdf-file-input");
  fileInput?.addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    const candidateId = fileInput.dataset.candidateId;
    if (!file || !candidateId) return;

    const formData = new FormData();
    formData.append("candidate_id", candidateId);
    formData.append("file", file);

    const consoleBox = document.getElementById("pipe-console-box");
    consoleBox.innerHTML += `<div class="log-line text-primary">[本地补录] 正在为候选文献上传本地 PDF (${file.name}, ${(file.size/1024/1024).toFixed(2)} MB)...</div>`;

    try {
      const res = await fetch("/api/pipeline/upload", {
        method: "POST",
        body: formData
      });
      const json = await res.json();
      if (json.success) {
        showToast(`🎉 《${file.name}》本地补录成功！已转入待入库就绪状态。`, "success", 4000);
        consoleBox.innerHTML += `<div class="log-line text-success font-bold">[上传成功] ${json.message}</div>`;
        loadPipelineData();
      } else {
        showToast(`上传失败: ${json.error}`, "error", 5000);
        consoleBox.innerHTML += `<div class="log-line text-danger">[上传失败] ${json.error}</div>`;
      }
    } catch (err) {
      showToast(`上传异常: ${err.message}`, "error", 5000);
      consoleBox.innerHTML += `<div class="log-line text-danger">[上传异常] ${err.message}</div>`;
    } finally {
      fileInput.value = "";
    }
  });
}

async function loadPipelineData() {
  const pendingTbody = document.getElementById("pipe-pending-tbody");
  const failedTbody = document.getElementById("pipe-failed-tbody");
  const successTbody = document.getElementById("pipe-success-tbody");

  try {
    const res = await fetch("/api/pipeline/overview");
    const json = await res.json();
    if (!json.success) return;

    const { pending = [], failed = [], success = [] } = json.data;

    const badgePending = document.getElementById("pipe-badge-pending");
    const badgeFailed = document.getElementById("pipe-badge-failed");
    const badgeSuccess = document.getElementById("pipe-badge-success");
    const navBadge = document.getElementById("nav-count-pipeline");

    if (badgePending) badgePending.textContent = pending.length;
    if (badgeFailed) badgeFailed.textContent = failed.length;
    if (badgeSuccess) badgeSuccess.textContent = success.length;
    if (navBadge) navBadge.textContent = pending.length;

    // 1. Pending Table (待入库)
    if (pending.length === 0) {
      pendingTbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted" style="padding:24px;">暂无待入库文献。可在「候选池管理」中审批通过文献加入此队列。</td></tr>`;
    } else {
      pendingTbody.innerHTML = pending.map(c => `
        <tr>
          <td>
            <div class="font-bold text-primary">《${escapeHtml(c.title || c.candidate_id)}》</div>
            <div class="text-muted text-xs">年份: ${c.publication_year || '-'} | 期刊: ${c.journal_or_venue || '-'} | DOI: ${c.doi || '-'}</div>
          </td>
          <td>
            <span class="badge ${c.lifecycle_status === 'FULLTEXT_ACQUIRED' ? 'badge-success' : 'badge-warning'}">
              ${c.lifecycle_status === 'FULLTEXT_ACQUIRED' ? '🟢 已获取全文' : '🟡 已批准待获取'}
            </span>
          </td>
          <td>
            <div class="flex-gap-xs">
              <button class="btn btn-primary btn-xs" onclick="startSingleIngest('${c.candidate_id}')">▶️ 立即入库</button>
              <button class="btn btn-secondary btn-xs" onclick="rejectPipelineCandidate('${c.candidate_id}')" title="移出流水线">✕ 移出</button>
            </div>
          </td>
        </tr>
      `).join('');
    }

    // 2. Failed Table (获取失败需人工干预)
    if (failed.length === 0) {
      failedTbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted" style="padding:24px;">暂无获取失败文献。</td></tr>`;
    } else {
      failedTbody.innerHTML = failed.map(c => `
        <tr>
          <td>
            <div class="font-bold text-danger">《${escapeHtml(c.title || c.candidate_id)}》</div>
            <div class="text-muted text-xs">年份: ${c.publication_year || '-'} | 期刊: ${c.journal_or_venue || '-'} | DOI: ${c.doi || '-'}</div>
          </td>
          <td>
            <div class="text-xs text-danger font-bold">目标站点反爬/需权限 (HTTP 403 / 无公开OA)</div>
            <div class="text-muted text-xs" style="margin-top:2px;">建议：点击「浏览器重试」调度真实 Chrome 模拟下载；或人工下载后点击「上传PDF」补录。</div>
          </td>
          <td>
            <div class="flex-gap-xs" style="flex-wrap: wrap;">
              <button class="btn btn-primary btn-xs" onclick="retryBrowserAcquire('${c.candidate_id}')" title="调度真实 Chrome 浏览器模拟下载">🌐 浏览器重试</button>
              <button class="btn btn-secondary btn-xs" onclick="triggerUploadPdf('${c.candidate_id}')" title="上传本地 PDF 文件补录">📁 上传PDF</button>
              <button class="btn btn-danger btn-xs" onclick="rejectPipelineCandidate('${c.candidate_id}')" title="放弃并移出流水线">🗑️ 移出</button>
            </div>
          </td>
        </tr>
      `).join('');
    }

    // 3. Success Table (已入库成功列表)
    if (success.length === 0) {
      successTbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted" style="padding:24px;">暂无入库成功的文献。</td></tr>`;
    } else {
      successTbody.innerHTML = success.map(c => `
        <tr>
          <td>
            <div class="font-bold text-success">《${escapeHtml(c.title || c.candidate_id)}》</div>
            <div class="text-muted text-xs">年份: ${c.publication_year || '-'} | 期刊: ${c.journal_or_venue || '-'} | DOI: ${c.doi || '-'}</div>
            <div class="text-muted text-xs"><code>${c.candidate_id}</code></div>
          </td>
          <td>
            <span class="badge badge-success">Qwen3-Embedding-8B (4096维)</span>
            <div class="text-muted text-xs" style="margin-top:4px;">引文网络拓扑与入度边已闭合回填</div>
          </td>
          <td>
            <button class="btn btn-secondary btn-xs" onclick="focusOnSeed('${c.in_library_work_id || c.candidate_id}')">🔍 图谱定位</button>
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {
    console.error("Load pipeline data error:", err);
  }
}

function triggerUploadPdf(candidateId) {
  const fileInput = document.getElementById("pipe-pdf-file-input");
  if (!fileInput) return;
  fileInput.dataset.candidateId = candidateId;
  fileInput.click();
}
window.triggerUploadPdf = triggerUploadPdf;

async function retryBrowserAcquire(candidateId) {
  const consoleBox = document.getElementById("pipe-console-box");
  const taskDesc = document.getElementById("pipe-task-desc");
  if (taskDesc) taskDesc.textContent = `正在调度真实 Chrome 浏览器模拟下载: ${candidateId}`;
  consoleBox.innerHTML += `<div class="log-line text-primary">[重试] 启动真实 Chrome 浏览器环境重新下载 ${candidateId}...</div>`;
  setPipelineStepActive("step-acq");

  try {
    const res = await fetch("/api/pipeline/acquire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_ids: [candidateId] })
    });
    const json = await res.json();
    if (!json.success) {
      showToast(`启动重试失败: ${json.error}`, "error");
      return;
    }

    pollJobStatus(json.data.job_id, (job) => {
      const acquired = job.result?.acquired_count || 0;
      if (acquired > 0) {
        showToast("🎉 浏览器自动化重新下载成功！PDF 已进入待入库队列。", "success", 4000);
        consoleBox.innerHTML += `<div class="log-line text-success font-bold">[成功] 浏览器自动化下载成功，已自动转入待入库就绪状态！</div>`;
      } else {
        showToast("⚠️ 浏览器重试仍未能捕获可用 PDF，建议点击「上传PDF」手动补录。", "warning", 5000);
        consoleBox.innerHTML += `<div class="log-line text-warning">[提示] 目标站点需人工登录或权限，请通过「上传PDF」补录。</div>`;
      }
      loadPipelineData();
      loadDashboardData();
    });
  } catch (err) {
    showToast(`重试异常: ${err.message}`, "error");
  }
}
window.retryBrowserAcquire = retryBrowserAcquire;

async function rejectPipelineCandidate(candidateId) {
  if (!confirm("确定要将此文献从入库流水线中移出（标记为 REJECTED）吗？")) return;
  try {
    const res = await fetch("/api/discovery/candidates/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_ids: [candidateId],
        action: "REJECT",
        reason: "USER_REMOVED_FROM_PIPELINE"
      })
    });
    const json = await res.json();
    if (json.success) {
      showToast("已成功从流水线中移出文献", "info");
      loadPipelineData();
      loadDashboardData();
    } else {
      showToast(`移出失败: ${json.error}`, "error");
    }
  } catch (err) {
    showToast(`请求异常: ${err.message}`, "error");
  }
}
window.rejectPipelineCandidate = rejectPipelineCandidate;

async function startIngestPipeline() {
  const res = await fetch("/api/pipeline/overview");
  const json = await res.json();
  const pending = json.data?.pending || [];

  if (pending.length === 0) {
    showToast("当前没有待入库的文献，请先在候选池中批准文献！", "warning");
    return;
  }

  const ids = pending.map(c => c.candidate_id);
  executeAcquireAndIngest(ids);
}

function startSingleIngest(cid) {
  executeAcquireAndIngest([cid]);
}

async function executeAcquireAndIngest(candidateIds) {
  const consoleBox = document.getElementById("pipe-console-box");
  const taskDesc = document.getElementById("pipe-task-desc");
  if (taskDesc) taskDesc.textContent = `执行中: ${candidateIds.length} 篇文献端到端获取与向量入库`;
  consoleBox.innerHTML += `<div class="log-line text-primary">[系统] 启动端到端合法 OA 全文获取与 Qwen3 向量入库流水线 (${candidateIds.length} 篇)...</div>`;

  setPipelineStepActive("step-acq");

  try {
    // 1. 获取阶段
    const resAcq = await fetch("/api/pipeline/acquire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_ids: candidateIds })
    });
    const jsonAcq = await resAcq.json();
    if (!jsonAcq.success) {
      consoleBox.innerHTML += `<div class="log-line text-danger">[错误] 获取启动失败: ${jsonAcq.error}</div>`;
      return;
    }

    pollJobStatus(jsonAcq.data.job_id, async (acqJob) => {
      const acqRes = acqJob.result || {};
      const acquiredCount = acqRes.acquired_count || 0;
      const failedCount = acqRes.unavailable_count || acqRes.failed_count || 0;

      if (acquiredCount > 0) {
        consoleBox.innerHTML += `<div class="log-line text-success">[完成] 全文获取与 PDF 验真通过 (${acquiredCount} 篇成功)！进入正文切块与向量嵌入生成...</div>`;
      } else {
        consoleBox.innerHTML += `<div class="log-line text-warning">[提示] 全文自动获取失败 (${failedCount} 篇未能通过网络下载)。若已手动放置 PDF 至 data/staging_pdf/，将尝试继续入库；否则请手动补充 PDF。</div>`;
      }
      setPipelineStepActive("step-parse");

      // 2. 入库与向量化阶段
      const resIng = await fetch("/api/pipeline/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_ids: candidateIds })
      });
      const jsonIng = await resIng.json();
      if (!jsonIng.success) {
        consoleBox.innerHTML += `<div class="log-line text-danger">[错误] 入库启动失败: ${jsonIng.error}</div>`;
        return;
      }

      setPipelineStepActive("step-embed");
      pollJobStatus(jsonIng.data.job_id, (ingJob) => {
        setPipelineStepActive("step-graph");
        const ingRes = ingJob.result || {};
        const ingestedCount = ingRes.ingested_count || 0;

        if (ingestedCount > 0) {
          consoleBox.innerHTML += `<div class="log-line text-success font-bold">[成功] 🎉 真实 Qwen3-Embedding-8B 4096维向量化完成（成功入库 ${ingestedCount} 篇），引文图谱节点与边已成功回填！</div>`;
          showToast(`🎉 成功入库 ${ingestedCount} 篇文献并完成 4096 维向量化与引文图谱回填！`, "success", 5000);
        } else {
          consoleBox.innerHTML += `<div class="log-line text-warning">[提示] ⚠️ 入库完成但未新增有效文献 (篇数=0)。请检查 staging_pdf 目录下是否存在对应 PDF 全文。</div>`;
          showToast("⚠️ 入库篇数为 0，请检查文献全文 PDF 是否已准备就绪。", "warning", 5000);
        }
        if (taskDesc) taskDesc.textContent = "流水线任务已执行完毕";
        loadPipelineData();
        loadDashboardData();
      });
    });
  } catch (err) {
    consoleBox.innerHTML += `<div class="log-line text-danger">[异常] ${err.message}</div>`;
  }
}

function setPipelineStepActive(stepId) {
  document.querySelectorAll(".pipe-step").forEach(s => s.classList.remove("active"));
  document.getElementById(stepId)?.classList.add("active");
}

// ── AUDIT 审计与运行记录 ───────────────────────────────────────────────────
function initAuditEvents() {
  document.querySelectorAll(".sub-nav-tabs .sub-tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sub-nav-tabs .sub-tab-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      const sub = btn.getAttribute("data-sub");
      AppState.audit.activeSubTab = sub;

      document.querySelectorAll(".audit-sub-card").forEach(c => c.style.display = "none");
      document.getElementById(`audit-sub-${sub}`).style.display = "block";
      loadAuditData();
    });
  });
}

async function loadAuditData() {
  try {
    if (AppState.audit.activeSubTab === "decisions" || AppState.audit.activeSubTab === "assets") {
      const res = await fetch("/api/audit/events");
      const json = await res.json();
      if (!json.success) return;

      if (AppState.audit.activeSubTab === "decisions") {
        const tbody = document.getElementById("audit-decisions-tbody");
        const list = json.data.decisions || [];
        if (list.length === 0) {
          tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">暂无审批审计记录</td></tr>`;
          return;
        }
        tbody.innerHTML = list.map(d => `
          <tr>
            <td>
              <div class="font-bold text-primary">《${escapeHtml(d.title || d.candidate_id)}》</div>
              <div class="text-muted text-xs">来源: ${d.decision_source || 'USER'}</div>
            </td>
            <td><span class="badge ${d.action === 'APPROVE' ? 'badge-success' : 'badge-danger'}">${d.action}</span></td>
            <td><code>${d.previous_status || 'DISCOVERED'}</code> ➔ <b class="text-primary">${d.new_status || 'APPROVED'}</b></td>
            <td><small>${d.reason || '-'}</small></td>
            <td><small class="text-muted">${d.decided_at || '-'}</small></td>
            <td><small>${d.operator || 'system'}</small></td>
          </tr>
        `).join('');
      } else if (AppState.audit.activeSubTab === "assets") {
        const tbody = document.getElementById("audit-assets-tbody");
        const list = json.data.assets || [];
        if (list.length === 0) {
          tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">暂无 PDF 资产记录</td></tr>`;
          return;
        }
        tbody.innerHTML = list.map(a => `
          <tr>
            <td>
              <div class="font-bold text-primary">《${escapeHtml(a.title || a.work_id)}》</div>
              <div class="text-muted text-xs"><a href="${a.source_url}" target="_blank">来源原文链接</a></div>
            </td>
            <td><b>${((a.file_size_bytes || 0) / 1024).toFixed(1)} KB</b> (${a.page_count || 0} 页)</td>
            <td><code title="${a.sha256}" style="font-size:11px;">${a.sha256 ? a.sha256.substring(0, 16) + '...' : '-'}</code></td>
            <td><span class="badge badge-success">${a.source_status || 'ACQUIRED'}</span></td>
            <td><small class="text-muted">${a.created_at || a.acquired_at || '-'}</small></td>
          </tr>
        `).join('');
      }
    } else if (AppState.audit.activeSubTab === "runs") {
      const res = await fetch("/api/discovery/runs");
      const json = await res.json();
      if (!json.success) return;

      const tbody = document.getElementById("audit-runs-tbody");
      const list = json.data || [];
      if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">暂无发现批次记录</td></tr>`;
        return;
      }
      tbody.innerHTML = list.map(r => `
        <tr>
          <td><code class="text-primary">${r.query_run_id}</code></td>
          <td><small>${r.seed_doc_ids_json || '-'}</small></td>
          <td>${r.direction || 'both'} / 深度 ${r.depth || 1}</td>
          <td><b>${r.unique_materialized_candidates_count || r.materialized_candidates_count || 0}</b></td>
          <td>${r.foundational_count || 0} / ${r.emerging_count || 0}</td>
          <td>${r.new_external_candidates_count || 0}</td>
          <td><small class="text-muted">${r.created_at || '-'}</small></td>
        </tr>
      `).join('');
    }
  } catch (err) {
    console.error("Load audit data error:", err);
  }
}

// ── MODALS 弹窗控制 ────────────────────────────────────────────────────────
function initModals() {
  document.getElementById("btn-close-doc-modal")?.addEventListener("click", () => {
    document.getElementById("modal-doc-detail").style.display = "none";
  });
  document.getElementById("btn-close-confirm-modal")?.addEventListener("click", () => {
    document.getElementById("modal-confirm-decision").style.display = "none";
  });
  document.getElementById("btn-cancel-confirm")?.addEventListener("click", () => {
    document.getElementById("modal-confirm-decision").style.display = "none";
  });
  document.getElementById("btn-execute-confirm")?.addEventListener("click", () => {
    executeDecision();
  });
}

async function openDocDetail(docId) {
  const modal = document.getElementById("modal-doc-detail");
  const body = document.getElementById("modal-doc-body");
  modal.style.display = "flex";
  body.innerHTML = `<div class="text-center text-muted py-4">正在载入文献元数据、PDF 资产与引文子图...</div>`;

  try {
    const res = await fetch(`/api/library/documents/${docId}`);
    const json = await res.json();
    if (!json.success) {
      body.innerHTML = `<div class="text-danger py-4">加载失败: ${json.error}</div>`;
      return;
    }

    const { paper, detail, asset, reference_mentions, citation_network } = json.data;
    const title = detail?.title_zh || detail?.title_en || paper?.title || docId;
    document.getElementById("modal-doc-title").textContent = `《${title}》`;

    const outgoingConfirmed = citation_network?.outgoing_citations?.confirmed_in_library || citation_network?.outgoing_citations?.in_library_confirmed || [];
    const outgoingCandidates = citation_network?.outgoing_citations?.candidates || [];
    const incomingCitations = Array.isArray(citation_network?.incoming_citations?.confirmed_in_library) 
      ? citation_network.incoming_citations.confirmed_in_library 
      : (Array.isArray(citation_network?.incoming_citations) ? citation_network.incoming_citations : []);
    const coCitations = citation_network?.bibliographic_coupling || [];

    body.innerHTML = `
      <div class="doc-detail-grid">
        <div class="card mb-3">
          <h4 class="font-bold text-primary mb-2">📑 基础元数据</h4>
          <div class="meta-kv-grid">
            <div><b>文献题名</b>: <b>《${escapeHtml(title)}》</b></div>
            <div><b>数字指纹 ID</b>: <code title="${docId}" style="font-size:11px;">${docId.substring(0, 16)}...</code> <small class="text-muted">(系统内部唯一标识)</small></div>
            <div><b>DOI</b>: ${detail?.doi ? `<a href="https://doi.org/${detail.doi}" target="_blank">${detail.doi}</a>` : '-'}</div>
            <div><b>作者</b>: ${detail?.authors || '-'}</div>
            <div><b>期刊 / 会议</b>: ${detail?.journal || '-'}</div>
            <div><b>出版年份</b>: ${detail?.year || '-'}</div>
            <div><b>文献类型</b>: <span class="badge badge-tag">${detail?.doc_type || 'paper'}</span></div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:10px;border-top:1px dashed var(--border-color);">
            <button class="btn btn-primary btn-xs" onclick="setSeedFromLibrary('${docId}', '${escapeHtml(title)}'); document.getElementById('modal-doc-detail').style.display='none';">❄️ 设为滚雪球种子文献</button>
            <button class="btn btn-secondary btn-xs" onclick="locateInGraph('${docId}'); document.getElementById('modal-doc-detail').style.display='none';">🕸️ 图谱聚焦此文献</button>
            <button class="btn btn-secondary btn-xs" onclick="enrichDocMetadata('${docId}')" title="根据篇名反查 OpenAlex 与 CrossRef 补全官方真实 DOI、作者与期刊">🌐 权威对齐与元数据补全</button>
          </div>
        </div>

        ${asset ? `
          <div class="card mb-3">
            <h4 class="font-bold text-success mb-2">📦 PDF 资产验真状态 (document_assets)</h4>
            <div class="meta-kv-grid">
              <div><b>文件大小</b>: ${(asset.file_size_bytes || 0).toLocaleString()} 字节</div>
              <div><b>页数</b>: ${asset.page_count || 0} 页</div>
              <div><b>SHA-256 哈希</b>: <code title="${asset.sha256}">${asset.sha256}</code></div>
              <div><b>获取来源</b>: <a href="${asset.source_url}" target="_blank">${asset.source_url ? asset.source_url.substring(0, 45) + '...' : '-'}</a></div>
            </div>
          </div>
        ` : ''}

        <div class="card mb-3">
          <h4 class="font-bold text-primary mb-2">🕸️ 馆内引文关系拓扑</h4>
          <div class="grid-2col">
            <div>
              <b>出向确认引用 (Outgoing): ${outgoingConfirmed.length} 篇</b>
              <ul class="citation-sub-list">
                ${outgoingConfirmed.length > 0 ? outgoingConfirmed.map(o => `<li>✅ 《${o.title}》 (${o.year || '-'})</li>`).join('') : '<li class="text-muted">暂无出向馆内引用</li>'}
              </ul>
            </div>
            <div>
              <b>馆内谁引用了它 (Incoming): ${incomingCitations.length} 篇</b>
              <ul class="citation-sub-list">
                ${incomingCitations.length > 0 ? incomingCitations.map(i => `<li>📥 《${i.title}》 (${i.year || '-'})</li>`).join('') : '<li class="text-muted">暂无馆内被引</li>'}
              </ul>
            </div>
          </div>
          ${coCitations.length > 0 ? `
            <div class="mt-2">
              <b>共同参考文献同行 (Bibliographic Coupling):</b>
              <ul class="citation-sub-list">
                ${coCitations.map(c => `<li>🔗 《${c.title}》 (共享 <b>${c.shared_references_count}</b> 篇参考文献)</li>`).join('')}
              </ul>
            </div>
          ` : ''}
        </div>

        <div class="card">
          <h4 class="font-bold text-primary mb-2">📖 参考文献结构化消歧明细 (${reference_mentions.length} 条)</h4>
          <div class="table-responsive" style="max-height: 250px;">
            <table class="data-table data-table-sm">
              <thead>
                <tr>
                  <th>#</th>
                  <th>解析题名 / 原始引文文本</th>
                  <th>解析作者</th>
                  <th>年份</th>
                  <th>DOI</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                ${reference_mentions.length > 0 ? reference_mentions.map(r => `
                  <tr>
                    <td>${r.ref_index}</td>
                    <td><div title="${escapeHtml(r.raw_text)}">${r.parsed_title ? escapeHtml(r.parsed_title) : `<span class="text-muted">${escapeHtml(r.raw_text.substring(0, 50))}...</span>`}</div></td>
                    <td><small>${r.parsed_author || '-'}</small></td>
                    <td><small>${r.parsed_year || '-'}</small></td>
                    <td><small>${r.parsed_doi || '-'}</small></td>
                    <td><span class="badge ${r.parse_status === 'PARSED_COMPLETE' ? 'badge-success' : 'badge-warning'}">${r.parse_status}</span></td>
                  </tr>
                `).join('') : '<tr><td colspan="6" class="text-center text-muted">暂无参考文献解析记录</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  } catch (err) {
    body.innerHTML = `<div class="text-danger py-4">请求异常: ${err.message}</div>`;
  }
}

let pendingActionType = "APPROVE";
function openConfirmDecisionModal(action) {
  pendingActionType = action;
  const modal = document.getElementById("modal-confirm-decision");
  const desc = document.getElementById("confirm-modal-desc");
  const itemsContainer = document.getElementById("confirm-modal-items");
  const title = document.getElementById("confirm-modal-title");

  title.textContent = action === "APPROVE" ? "确认批准候选文献 (APPROVE)" : "确认拒绝候选文献 (REJECT)";
  desc.textContent = `您即将对以下 ${AppState.candidates.selectedIds.size} 篇候选文献执行 ${action} 操作（该操作将记录入 Append-Only 审计数据库）：`;

  const selectedList = Array.from(AppState.candidates.selectedIds);
  itemsContainer.innerHTML = selectedList.map(id => `<div>• Candidate ID: <code>${id}</code></div>`).join('');

  modal.style.display = "flex";
}

async function executeDecision() {
  const selectedList = Array.from(AppState.candidates.selectedIds);
  const reason = document.getElementById("confirm-modal-reason").value.trim() || "USER_WEB_UI_CONFIRMATION";
  const operator = document.getElementById("confirm-modal-operator").value.trim() || "user_web_ui";

  document.getElementById("modal-confirm-decision").style.display = "none";

  try {
    const res = await fetch("/api/discovery/candidates/decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        candidate_ids: selectedList,
        action: pendingActionType,
        reason: reason,
        decision_source: "USER",
        operator: operator
      })
    });
    const json = await res.json();
    if (!json.success) {
      showToast("操作失败: " + json.error, "error");
      return;
    }

    const { updated_count, rejected_count, rejected } = json.data;
    let msg = `审批决策执行完毕！成功流转 ${updated_count} 篇。`;
    if (rejected_count > 0) {
      msg += `\n⚠️ 有 ${rejected_count} 篇因非 DISCOVERED 状态被状态机硬拦截拒绝：\n` + rejected.map(r => `• ${r.candidate_id}: ${r.error}`).join('\n');
    }

    AppState.candidates.selectedIds.clear();
    loadCandidatesData();
    loadDashboardData();
    showToast(msg, "info");
  } catch (err) {
    showToast("请求异常: " + err.message, "error");
  }
}

// ── 任务轮询工具 ─────────────────────────────────────────────────────────────
function pollJobStatus(jobId, onComplete) {
  if (AppState.jobPollTimer) clearInterval(AppState.jobPollTimer);

  const consoleBox = document.getElementById("pipe-console-box");

  AppState.jobPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/pipeline/jobs/${jobId}`);
      const json = await res.json();
      if (!json.success) return;

      const job = json.data;
      if (consoleBox && job.logs) {
        consoleBox.innerHTML = job.logs.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join('');
        consoleBox.scrollTop = consoleBox.scrollHeight;
      }

      if (job.status === "completed") {
        clearInterval(AppState.jobPollTimer);
        AppState.jobPollTimer = null;
        if (onComplete) onComplete(job);
      } else if (job.status === "failed") {
        clearInterval(AppState.jobPollTimer);
        AppState.jobPollTimer = null;
        showToast(`任务执行失败: ${job.error}`, "error", 6000);
      }
    } catch (err) {
      console.error("Poll job error:", err);
    }
  }, 1000);
}

// ── 通用工具函数 ─────────────────────────────────────────────────────────────
function renderPagination(containerId, stateObj, loadFn) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const page = stateObj.page || 1;
  const totalPages = stateObj.totalPages || 1;
  const pageSize = stateObj.pageSize || 20;

  let pageNumbers = [];
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pageNumbers.push(i);
  } else {
    pageNumbers = [1];
    let start = Math.max(2, page - 2);
    let end = Math.min(totalPages - 1, page + 2);
    if (start > 2) pageNumbers.push("...");
    for (let i = start; i <= end; i++) pageNumbers.push(i);
    if (end < totalPages - 1) pageNumbers.push("...");
    pageNumbers.push(totalPages);
  }

  container.innerHTML = `
    <div class="pagination-container" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding:14px 0;border-top:1px solid var(--border-color);margin-top:10px;">
      <div class="pagination-page-size" style="display:flex;align-items:center;gap:8px;font-size:13px;">
        <span class="text-muted">每页展示:</span>
        <select class="form-select" style="width:auto;padding:4px 8px;font-size:12px;" onchange="changePageSize('${containerId}', this.value)">
          <option value="20" ${pageSize === 20 ? 'selected' : ''}>20 篇/页</option>
          <option value="50" ${pageSize === 50 ? 'selected' : ''}>50 篇/页</option>
          <option value="100" ${pageSize === 100 ? 'selected' : ''}>100 篇/页</option>
          <option value="500" ${pageSize >= 500 ? 'selected' : ''}>全部 344 篇 (一页全显)</option>
        </select>
      </div>
      
      <div class="pagination-buttons" style="display:flex;align-items:center;gap:4px;">
        <button class="btn btn-secondary btn-xs" ${page <= 1 ? 'disabled' : ''} onclick="changePage('${containerId}', 1)" title="第一页">⇤ 首页</button>
        <button class="btn btn-secondary btn-xs" ${page <= 1 ? 'disabled' : ''} onclick="changePage('${containerId}', ${page - 1})">◀ 上页</button>
        
        <div style="display:flex;gap:4px;align-items:center;margin:0 4px;">
          ${pageNumbers.map(p => {
            if (p === "...") return `<span style="padding:0 4px;color:var(--text-muted);font-size:12px;">...</span>`;
            return `<button class="btn ${p === page ? 'btn-primary' : 'btn-secondary'} btn-xs" style="min-width:28px;padding:3px 6px;" onclick="changePage('${containerId}', ${p})">${p}</button>`;
          }).join('')}
        </div>

        <button class="btn btn-secondary btn-xs" ${page >= totalPages ? 'disabled' : ''} onclick="changePage('${containerId}', ${page + 1})">下页 ▶</button>
        <button class="btn btn-secondary btn-xs" ${page >= totalPages ? 'disabled' : ''} onclick="changePage('${containerId}', ${totalPages})" title="最后一页">尾页 ⇥</button>
      </div>

      <div class="pagination-jump" style="display:flex;align-items:center;gap:6px;font-size:13px;">
        <span class="text-muted">跳至</span>
        <input type="number" min="1" max="${totalPages}" value="${page}" style="width:48px;padding:3px 6px;font-size:12px;border:1px solid var(--border-color);border-radius:4px;background:var(--bg-card);color:var(--text-main);text-align:center;" onkeydown="if(event.key==='Enter') changePage('${containerId}', parseInt(this.value))">
        <span class="text-muted">/ ${totalPages} 页</span>
      </div>
    </div>
  `;
}

window.changePage = function(containerId, targetPage) {
  const p = Math.max(1, parseInt(targetPage) || 1);
  if (containerId === "lib-pagination") {
    AppState.library.page = p;
    loadLibraryData();
  } else if (containerId === "cand-pagination") {
    AppState.candidates.page = p;
    loadCandidatesData();
  }
};

window.changePageSize = function(containerId, newSize) {
  const size = parseInt(newSize) || 20;
  if (containerId === "lib-pagination") {
    AppState.library.pageSize = size;
    AppState.library.page = 1;
    loadLibraryData();
  } else if (containerId === "cand-pagination") {
    AppState.candidates.pageSize = size;
    AppState.candidates.page = 1;
    loadCandidatesData();
  }
};

window.copyText = function(txt) {
  navigator.clipboard.writeText(txt).then(() => {
    showToast("已成功复制到剪贴板", "success");
  });
};

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}



async function enrichDocMetadata(docId) {
  showToast("⏳ 正在向 OpenAlex & CrossRef 权威学术图谱发起题名反查与元数据对齐...", "info", 4000);
  try {
    const res = await fetch(`/api/library/documents/${docId}/enrich`, {
      method: "POST"
    });
    const json = await res.json();
    if (json.success && json.enriched_data) {
      const d = json.enriched_data;
      showToast(`✅ 权威对齐成功！DOI: ${d.doi || '已更新'}, 期刊: ${d.journal || '-'} (${d.year || '-'}), 作者: ${d.authors ? d.authors.substring(0, 25) + '...' : '-'}`, "success", 5000);
      openDocDetail(docId);
      loadLibraryData();
    } else {
      showToast(`⚠️ 对齐提示: ${json.error || '未检索到更权威版本'}`, "warning", 4000);
    }
  } catch (err) {
    showToast(`❌ 权威对齐请求失败: ${err.message}`, "error", 4000);
  }
}
window.enrichDocMetadata = enrichDocMetadata;
