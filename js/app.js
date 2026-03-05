/**
 * Cache API Dashboard Application
 */

// Configuration
function resolveApiBaseUrl() {
    // Allow explicit override for non-default deployments.
    const queryOverride = new URLSearchParams(window.location.search).get('apiBaseUrl');
    if (queryOverride) {
        return queryOverride.replace(/\/+$/, '');
    }

    // If dashboard is served over HTTP(S), prefer same-origin to avoid mixed/blocked requests.
    const protocol = window.location.protocol;
    if (protocol === 'http:' || protocol === 'https:') {
        return window.location.origin;
    }

    // Fallback for file:// or unknown contexts.
    return 'http://localhost:5000';
}

const CONFIG = {
    apiBaseUrl: resolveApiBaseUrl(),
    checkInterval: 30000 // 30 seconds
};

// State
let state = {
    isLoggedIn: false,
    activeTab: 'overview',
    stats: {
        activeTokens: 0,
        connectedApis: 0,
        health: 'Unknown'
    }
};

// DOM Elements
const elements = {
    loginModal: document.getElementById('loginModal'),
    mainDashboard: document.getElementById('mainDashboard'),
    logoutBtn: document.getElementById('logoutBtn'),
    loginError: document.getElementById('loginError'),
    adminPassword: document.getElementById('adminPassword'),
    tabButtons: document.querySelectorAll('.tab-button'),
    tabContents: document.querySelectorAll('.tab-content'),
    testerUrl: document.getElementById('testerUrl'),
    testerMethod: document.getElementById('testerMethod'),
    testerBody: document.getElementById('testerBody'),
    testerToken: document.getElementById('testerToken'),
    responseBody: document.getElementById('responseBody'),
    responseStatus: document.getElementById('responseStatus'),
    statusCode: document.getElementById('statusCode'),
    statusText: document.getElementById('statusText'),
    logsTableBody: document.getElementById('logsTableBody'),
    logsStats: document.getElementById('logsStats'),
    sessionsTableBody: document.getElementById('sessionsTableBody')
};

/**
 * Initialization
 */
document.addEventListener('DOMContentLoaded', () => {
    // Check if previously logged in
    const storedToken = localStorage.getItem('adminToken');
    if (storedToken) {
        showDashboard();
    }

    // Initialize tabs
    elements.tabButtons.forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // Set default token in tester
    if (elements.testerToken && storedToken) {
        elements.testerToken.value = storedToken;
    }
    
    // Setup enter key for login
    if (elements.adminPassword) {
        elements.adminPassword.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') handleLogin();
        });
    }

    // Auto-refresh stats
    setInterval(() => {
        if (state.isLoggedIn) updateStats();
    }, CONFIG.checkInterval);
});

/**
 * Authentication
 */
async function handleLogin() {
    const token = elements.adminPassword.value.trim();
    
    if (!token) {
        showLoginError('Please enter a token');
        return;
    }

    elements.loginError.style.display = 'none';
    const loginBtn = document.querySelector('#loginModal .btn-primary');
    const originalText = loginBtn.textContent;
    loginBtn.textContent = 'Verifying...';
    loginBtn.disabled = true;

    try {
        // Verify token against sessions endpoint
        const response = await fetch(`${CONFIG.apiBaseUrl}/admin/sessions`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (response.ok) {
            localStorage.setItem('adminToken', token);
            localStorage.setItem('isLoggedIn', 'true');
            if (elements.testerToken) elements.testerToken.value = token;

            // Also set the server-side cookie so cookie-aware flows stay in sync.
            try {
                const form = new URLSearchParams();
                form.append('admin_token', token);
                await fetch(`${CONFIG.apiBaseUrl}/admin/dashboard/login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: form.toString(),
                    redirect: 'manual',   // don't follow the 303, we stay on this page
                });
            } catch (_) { /* non-critical */ }

            showDashboard();
        } else {
            showLoginError('Invalid API Token');
        }
    } catch (error) {
        showLoginError(`Network error: ${error.message}`);
    } finally {
        loginBtn.textContent = originalText;
        loginBtn.disabled = false;
    }
}

function showDashboard() {
    state.isLoggedIn = true;
    elements.loginModal.style.display = 'none';
    elements.loginModal.classList.remove('active');
    elements.mainDashboard.style.display = 'block';
    
    // Show logout button
    if (elements.logoutBtn) elements.logoutBtn.style.display = 'block';
    
    // Setup logout handler
    if (elements.logoutBtn) elements.logoutBtn.onclick = handleLogout;

    // Initial data fetch
    updateStats();
}

function handleLogout() {
    state.isLoggedIn = false;
    localStorage.removeItem('isLoggedIn');
    localStorage.removeItem('adminToken');
    // Expire the server-side cookie so the server state is also cleared.
    document.cookie = 'admin_access=; Max-Age=0; path=/; Secure; SameSite=Strict';
    window.location.reload();
}

function showLoginError(msg) {
    elements.loginError.textContent = msg;
    elements.loginError.style.display = 'block';
}

/**
 * Navigation
 */
function switchTab(tabId) {
    // Update state
    state.activeTab = tabId;
    
    // Update UI
    elements.tabButtons.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    
    elements.tabContents.forEach(content => {
        content.classList.toggle('active', content.id === `${tabId}Tab`);
    });
    
    // Tab specific actions
    if (tabId === 'logs') {
        fetchLogs();
    } else if (tabId === 'sessions') {
        fetchSessions();
    } else if (tabId === 'overview') {
        updateStats();
    } else if (tabId === 'missing') {
        fetchMissingData();
    } else if (tabId === 'tokens') {
        fetchTokens();
    } else if (tabId === 'analytics') {
        fetchAnalytics();
    }
}

/**
 * Data Fetching
 */
async function updateStats() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;

    try {
        const [sessionsRes, cacheRes] = await Promise.all([
            fetch(`${CONFIG.apiBaseUrl}/admin/sessions`, { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch(`${CONFIG.apiBaseUrl}/admin/stats/cache`, { headers: { 'Authorization': `Bearer ${token}` } })
        ]);

        // Process Session Data
        if (sessionsRes.ok) {
            const data = await sessionsRes.json();
            
            // Stats Cards
            updateElementText('activeSessionsCount', data.total_sessions || 0);
            updateElementText('totalRequestsCount', data.total_tracked_requests || 0);
            
            // Overview Table
            updateElementText('statTotalRequests', data.total_tracked_requests);
            updateElementText('statTotalSessions', data.total_sessions);
            updateElementText('statAdminSessions', data.admin_sessions);
            updateElementText('statUserSessions', data.non_admin_sessions);
            
            // Cache session data for sessions tab
            if (state.activeTab === 'sessions' && data.sessions) {
                renderSessions(data.sessions);
            }
        }

        // Process Cache Data
        if (cacheRes.ok) {
            const data = await cacheRes.json();
            
            // Stats Card
            const statusEl = document.getElementById('redisStatus');
            if (statusEl) {
                statusEl.textContent = data.status === 'online' ? 'Online' : 'Offline';
                statusEl.className = 'stat-value ' + (data.status === 'online' ? 'text-success' : 'text-error');
            }

            // Overview Table
            updateElementText('statRedisStatus', data.status);
            updateElementText('statRedisKeys', data.total_cache_keys);
            updateElementText('statRedisMemory', data.used_memory_human);
            updateElementText('statRedisVersion', data.redis_version);
        }

        updateElementText('lastUpdated', new Date().toLocaleTimeString());

    } catch (e) {
        console.error("Stats update failed", e);
    }
}

async function fetchSessions() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;

    const tbody = document.getElementById('sessionsTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading sessions...</td></tr>';

    try {
        const response = await fetch(`${CONFIG.apiBaseUrl}/admin/sessions`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const data = await response.json();
            renderSessions(data.sessions);
        }
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="6" class="text-center text-error">Error: ${e.message}</td></tr>`;
    }
}

function renderSessions(sessions) {
    const tbody = document.getElementById('sessionsTableBody');
    if (!tbody) return;

    if (!sessions || sessions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center">No active sessions</td></tr>';
        return;
    }

    const html = sessions.map(session => {
        const created = new Date(session.created_at).toLocaleString();
        const lastActive = new Date(session.last_activity).toLocaleString();
        
        return `
            <tr>
                <td class="font-mono">${session.session_id.substring(0, 8)}...</td>
                <td>${session.ip_address || 'Unknown'}</td>
                <td><span class="badge ${session.token_type === 'admin' ? 'bg-primary' : ''}">${session.token_type}</span></td>
                <td><small>${created}</small></td>
                <td><small>${lastActive}</small></td>
                <td>${session.request_count}</td>
            </tr>
        `;
    }).join('');

    tbody.innerHTML = html;
}

/**
 * Logs Management
 */
async function fetchLogs() {
    if (!elements.logsTableBody) return;
    
    const limit = document.getElementById('logsLimit')?.value || 50;
    const token = localStorage.getItem('adminToken');
    
    if (!token) {
        handleLogout();
        return;
    }

    elements.logsTableBody.innerHTML = '<tr><td colspan="6" class="text-center">Loading logs...</td></tr>';
    
    try {
        const response = await fetch(`${CONFIG.apiBaseUrl}/admin/logs?limit=${limit}`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });
        
        if (response.status === 401 || response.status === 403) {
            handleLogout();
            return;
        }
        
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        renderLogs(data.requests, data.total);
        
    } catch (error) {
        elements.logsTableBody.innerHTML = `<tr><td colspan="6" class="text-center text-error">Failed to fetch logs: ${error.message}</td></tr>`;
    }
}

function renderLogs(logs, total) {
    if (!logs || logs.length === 0) {
        elements.logsTableBody.innerHTML = '<tr><td colspan="6" class="text-center">No logs found</td></tr>';
        return;
    }
    
    if (elements.logsStats) {
        elements.logsStats.textContent = `Showing ${logs.length} of ${total} total logs`;
    }
    
    const html = logs.map(log => {
        const date = new Date(log.timestamp).toLocaleString();
        const duration = log.response_time_ms ? `${log.response_time_ms.toFixed(2)}ms` : '-';
        const statusClass = log.response_status >= 400 ? 'status-error' : 'status-success';
        const location = log.location || 'Unknown';
        const tokenMasked = log.token_masked || 'None';
        
        return `
            <tr>
                <td>${date}</td>
                <td><span class="badge">${log.method}</span></td>
                <td class="font-mono" title="${log.path}">${truncate(log.path, 30)}</td>
                <td><span class="status-badge ${statusClass}">${log.response_status}</span></td>
                <td>${duration}</td>
                <td>
                    <div><small><strong>IP:</strong> ${log.ip_address} (${location})</small></div>
                    <div><small><strong>Token:</strong> ${tokenMasked}</small></div>
                </td>
            </tr>
        `;
    }).join('');
    
    elements.logsTableBody.innerHTML = html;
}

/**
 * API Tester
 */
function setEndpoint(method, path, event) {
    if (event) event.preventDefault();
    
    elements.testerMethod.value = method;
    elements.testerUrl.value = `${CONFIG.apiBaseUrl}${path}`;
    
    // Auto-fill body for batch request
    if (path.includes('batch') && method === 'POST') {
        loadBatchExample();
    }
}

function loadBatchExample() {
    if (elements.testerBody) {
        elements.testerBody.value = JSON.stringify({
            "team": ["sea", "ne"],
            "player": ["Cooper Kupp"],
            "market": ["Rush + Rec Yards"]
        }, null, 2);
    }
}

function clearTesterForm() {
    elements.testerUrl.value = CONFIG.apiBaseUrl;
    elements.testerBody.value = '';
    elements.responseBody.innerHTML = '<div style="text-align: center; color: #999; padding: 40px;">Send a request to see the response here</div>';
    elements.responseStatus.style.display = 'none';
}

async function sendApiRequest(event) {
    if (event) event.preventDefault();
    
    const method = elements.testerMethod.value;
    const url = elements.testerUrl.value;
    const token = elements.testerToken.value;
    const bodyStr = elements.testerBody.value;
    
    // UI Loading state
    elements.responseBody.innerHTML = '<div class="loading-spinner">Sending request...</div>';
    elements.responseStatus.style.display = 'none';
    
    try {
        const headers = {
            'Content-Type': 'application/json'
        };
        
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }
        
        const options = {
            method,
            headers
        };
        
        if (method !== 'GET' && method !== 'HEAD' && bodyStr) {
            try {
                options.body = JSON.stringify(JSON.parse(bodyStr));
            } catch (e) {
                alert('Invalid JSON in request body');
                return;
            }
        }
        
        const startTime = performance.now();
        const response = await fetch(url, options);
        const duration = performance.now() - startTime;
        
        // Handle response
        const status = response.status;
        const statusText = response.statusText;
        let responseData;
        
        const contentType = response.headers.get("content-type");
        if (contentType && contentType.indexOf("application/json") !== -1) {
            responseData = await response.json();
            elements.responseBody.innerHTML = `<pre>${JSON.stringify(responseData, null, 2)}</pre>`;
        } else {
            responseData = await response.text();
            elements.responseBody.innerHTML = `<pre>${responseData}</pre>`;
        }
        
        // Update status UI
        elements.statusCode.textContent = status;
        elements.statusText.textContent = `${statusText} (${duration.toFixed(0)}ms)`;
        
        elements.responseStatus.className = 'response-status ' + (status >= 400 ? 'status-error' : 'status-success');
        elements.responseStatus.style.display = 'flex';
        
    } catch (error) {
        elements.responseBody.innerHTML = `<div class="alert alert-error">Request failed: ${error.message}</div>`;
    }
}

/**
 * Utils
 */
function updateElementText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text !== undefined && text !== null ? text : '-';
}

function truncate(str, n) {
    if (!str) return '';
    return (str.length > n) ? str.substr(0, n-1) + '...' : str;
}

function copyToClipboard(elementId) {
    const el = document.getElementById(elementId);
    if (el) {
        el.select();
        document.execCommand('copy');
    }
}

/**
 * Missing Data Functions
 */
async function fetchMissingData() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;

    const typeFilter = document.getElementById('missingTypeFilter')?.value || '';
    const sortBy = document.getElementById('missingSortBy')?.value || 'last_seen';
    const limit = parseInt(document.getElementById('missingLimit')?.value || '50');

    try {
        const params = new URLSearchParams({
            limit: limit,
            offset: 0,
            sort_by: sortBy
        });

        if (typeFilter) {
            params.append('item_type', typeFilter);
        }

        const response = await fetch(`${CONFIG.apiBaseUrl}/admin/missing-items?${params}`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to fetch missing data');
        }

        const data = await response.json();
        displayMissingData(data);

    } catch (error) {
        console.error('Error fetching missing data:', error);
        const tbody = document.getElementById('missingDataTableBody');
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="7" class="text-center" style="color: #f44336;">Error loading data: ${error.message}</td></tr>`;
        }
    }
}

function displayMissingData(data) {
    const tbody = document.getElementById('missingDataTableBody');
    const summaryDiv = document.getElementById('missingStatsByType');

    // Update summary statistics
    document.getElementById('missingTotalUnique').textContent = data.total || 0;

    let totalOccurrences = 0;
    if (data.stats_by_type) {
        Object.values(data.stats_by_type).forEach(stat => {
            totalOccurrences += stat.total_occurrences || 0;
        });
    }
    document.getElementById('missingTotalOccurrences').textContent = totalOccurrences;

    // Display stats by type
    if (summaryDiv && data.stats_by_type) {
        summaryDiv.innerHTML = '';
        Object.entries(data.stats_by_type).forEach(([type, stats]) => {
            const typeCard = document.createElement('div');
            typeCard.style.cssText = 'padding: 10px; background: #f5f5f5; border-radius: 4px; border-left: 3px solid #2196F3;';
            typeCard.innerHTML = `
                <div style="font-weight: 600; text-transform: capitalize; margin-bottom: 5px;">${getTypeIcon(type)} ${type}</div>
                <div style="font-size: 0.9em; color: #666;">
                    Unique: ${stats.unique_count} | Total: ${stats.total_occurrences}
                </div>
            `;
            summaryDiv.appendChild(typeCard);
        });
    }

    // Display table data
    if (!tbody) return;

    if (!data.items || data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center" style="color: #999;">No missing data found</td></tr>';
        return;
    }

    tbody.innerHTML = data.items.map((item, index) => {
        const firstSeen = formatTimestamp(item.first_seen);
        const lastSeen = formatTimestamp(item.last_seen);
        const context = formatContext(item.query_params);

        return `
            <tr data-missing-index="${index}" style="cursor: pointer;" onclick="showMissingDetails(window._missingDataItems[${index}])">
                <td><span style="display: inline-block; padding: 2px 8px; background: ${getTypeColor(item.item_type)}; color: white; border-radius: 3px; font-size: 0.85em;">${getTypeIcon(item.item_type)} ${item.item_type}</span></td>
                <td style="font-weight: 500;">${escapeHtml(item.item_value)}</td>
                <td><code style="font-size: 0.9em;">${escapeHtml(item.endpoint)}</code></td>
                <td style="font-size: 0.9em; color: #666;">${context}</td>
                <td style="font-size: 0.9em;">${firstSeen}</td>
                <td style="font-size: 0.9em;">${lastSeen}</td>
                <td style="text-align: center;"><span style="display: inline-block; padding: 2px 8px; background: #ff9800; color: white; border-radius: 12px; font-size: 0.85em; font-weight: 600;">${item.occurrence_count}</span></td>
            </tr>
        `;
    }).join('');

    // Store items globally for click handler access
    window._missingDataItems = data.items;

    // Auto-select first row to immediately show details
    if (data.items.length > 0) {
        showMissingDetails(data.items[0]);
    }

    // Update pagination info
    const paginationDiv = document.getElementById('missingDataPagination');
    if (paginationDiv) {
        paginationDiv.textContent = `Showing ${data.items.length} of ${data.total} items`;
    }
}

function showMissingDetails(item) {
    if (!item) return;

    // Highlight selected row
    document.querySelectorAll('#missingDataTableBody tr').forEach(r => r.style.background = '');
    const idx = window._missingDataItems.indexOf(item);
    const selectedRow = document.querySelector(`#missingDataTableBody tr[data-missing-index="${idx}"]`);
    if (selectedRow) selectedRow.style.background = '#e3f2fd';

    // Populate IP Address
    const ipEl = document.getElementById('missingDetailIp');
    if (ipEl) ipEl.textContent = item.ip_address || '-';

    // Populate Missing Fields
    const fieldsEl = document.getElementById('missingDetailFields');
    if (fieldsEl) {
        const grouped = item.missing_fields_grouped || {};
        fieldsEl.textContent = Object.keys(grouped).length > 0
            ? JSON.stringify(grouped, null, 2)
            : '{}';
    }

    // Populate Body
    const bodyEl = document.getElementById('missingDetailBody');
    if (bodyEl) {
        const body = item.body_data || {};
        bodyEl.textContent = (typeof body === 'object' && Object.keys(body).length > 0)
            ? JSON.stringify(body, null, 2)
            : '{}';
    }

    // Hide the hint
    const hintEl = document.getElementById('missingDetailHint');
    if (hintEl) hintEl.style.display = 'none';
}

function getTypeIcon(type) {
    const icons = {
        'market': '📊',
        'team': '🏆',
        'player': '👤',
        'league': '🏅'
    };
    return icons[type] || '📌';
}

function getTypeColor(type) {
    const colors = {
        'market': '#2196F3',
        'team': '#4CAF50',
        'player': '#FF9800',
        'league': '#9C27B0'
    };
    return colors[type] || '#757575';
}

function formatContext(queryParams) {
    if (!queryParams || typeof queryParams !== 'object') return '-';

    const relevant = Object.entries(queryParams)
        .filter(([key, value]) => value && value !== null && value !== '')
        .map(([key, value]) => `${key}: ${value}`);

    if (relevant.length === 0) return '-';
    if (relevant.length <= 2) return relevant.join(', ');

    return relevant.slice(0, 2).join(', ') + '...';
}

function formatTimestamp(timestamp) {
    if (!timestamp) return '-';

    try {
        const date = new Date(timestamp);
        const now = new Date();
        const diff = now - date;

        if (diff < 60000) return 'Just now';
        if (diff < 3600000) {
            const mins = Math.floor(diff / 60000);
            return `${mins} min${mins > 1 ? 's' : ''} ago`;
        }
        if (diff < 86400000) {
            const hours = Math.floor(diff / 3600000);
            return `${hours} hour${hours > 1 ? 's' : ''} ago`;
        }
        if (diff < 604800000) {
            const days = Math.floor(diff / 86400000);
            return `${days} day${days > 1 ? 's' : ''} ago`;
        }
        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
    } catch (e) {
        return timestamp;
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showNotification(message, type) {
    const container = document.getElementById('notifications');
    if (!container) {
        alert(message);
        return;
    }
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.textContent = message;
    container.appendChild(notification);
    setTimeout(() => notification.remove(), 3000);
}

async function clearMissingData() {
    if (!confirm('Are you sure you want to clear all missing data records?')) {
        return;
    }

    const token = localStorage.getItem('adminToken');
    if (!token) return;

    try {
        const response = await fetch(`${CONFIG.apiBaseUrl}/admin/missing-items`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (!response.ok) {
            throw new Error('Failed to clear missing data');
        }

        showNotification('Missing data cleared successfully', 'success');
        fetchMissingData();

    } catch (error) {
        console.error('Error clearing missing data:', error);
        showNotification('Failed to clear missing data: ' + error.message, 'error');
    }
}

// ─── Token Management ──────────────────────────────────────────────────────────

async function fetchTokens() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;
    const tbody = document.getElementById('tokensTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="text-center">Loading...</td></tr>';
    try {
        const res = await fetch(`${CONFIG.apiBaseUrl}/admin/tokens`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderTokens(data.tokens || []);
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="10" class="text-center">${escapeHtml(e.message)}</td></tr>`;
    }
}

function renderTokens(tokens) {
    const tbody = document.getElementById('tokensTableBody');
    if (!tbody) return;

    const summary = document.getElementById('tokensSummary');
    if (summary) {
        const total = tokens.length;
        const active = tokens.filter(t => t.status === 'active').length;
        const admins = tokens.filter(t => t.role === 'admin' && t.status === 'active').length;
        const revoked = tokens.filter(t => t.status === 'revoked').length;
        summary.innerHTML = [
            { label: 'Total Tokens', value: total, color: '#4f46e5' },
            { label: 'Active', value: active, color: '#22c55e' },
            { label: 'Admin Tokens', value: admins, color: '#f59e0b' },
            { label: 'Revoked', value: revoked, color: '#ef4444' },
        ].map(c => `
            <div style="background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:16px; border-left:4px solid ${c.color};">
                <div style="font-size:0.8rem; color:#6b7280; margin-bottom:4px;">${c.label}</div>
                <div style="font-size:1.8rem; font-weight:700; color:${c.color};">${c.value}</div>
            </div>
        `).join('');
    }

    if (!tokens.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center">No tokens found</td></tr>';
        return;
    }

    tbody.innerHTML = tokens.map(t => {
        const statusBadge = t.status === 'active'
            ? `<span class="token-badge-active">active</span>`
            : `<span class="token-badge-revoked">revoked</span>`;
        const roleBadge = t.role === 'admin'
            ? `<span class="token-badge-admin">admin</span>`
            : `<span class="token-badge-user">user</span>`;
        const actions = t.status === 'active' ? `
            <button class="btn-token-action" onclick="revokeToken('${t.token_id}','${escapeHtml(t.name)}')" title="Revoke">\uD83D\uDEAB</button>
            <button class="btn-token-action" onclick="rotateToken('${t.token_id}','${escapeHtml(t.name)}')" title="Rotate">\uD83D\uDD04</button>
        ` : '';
        return `<tr>
            <td><strong>${escapeHtml(t.name)}</strong></td>
            <td>${escapeHtml(t.owner || '—')}</td>
            <td>${roleBadge}</td>
            <td><code style="font-size:0.85em;">${escapeHtml(t.token_masked)}</code></td>
            <td>${statusBadge}</td>
            <td><small>${formatTimestamp(t.created_at)}</small></td>
            <td><small>${t.last_used ? formatTimestamp(t.last_used) : '—'}</small></td>
            <td><small>${escapeHtml(t.last_ip || '—')}</small></td>
            <td><small>${escapeHtml(t.expires_at || '—')}</small></td>
            <td>${actions}</td>
        </tr>`;
    }).join('');
}

async function fetchTokenAudit(tokenId = null) {
    const token = localStorage.getItem('adminToken');
    if (!token) return;
    const tbody = document.getElementById('tokenAuditTableBody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading...</td></tr>';
    try {
        const url = tokenId
            ? `${CONFIG.apiBaseUrl}/admin/tokens/audit?token_id=${tokenId}&limit=50`
            : `${CONFIG.apiBaseUrl}/admin/tokens/audit?limit=100`;
        const res = await fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderAuditLog(data.audit || []);
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="6" class="text-center">${escapeHtml(e.message)}</td></tr>`;
    }
}

function renderAuditLog(entries) {
    const tbody = document.getElementById('tokenAuditTableBody');
    if (!tbody) return;
    if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center">No audit entries</td></tr>';
        return;
    }
    const actionColor = { created: '#22c55e', revoked: '#ef4444', rotated_out: '#f59e0b' };
    tbody.innerHTML = entries.map(e => `<tr>
        <td><small>${formatTimestamp(e.timestamp)}</small></td>
        <td><code style="font-size:0.85em;">${escapeHtml(e.token_masked || '—')}</code></td>
        <td><span style="padding:2px 8px; border-radius:4px; background:${actionColor[e.action] || '#6b7280'}; color:#fff; font-size:0.8em;">${escapeHtml(e.action)}</span></td>
        <td>${escapeHtml(e.actor || '—')}</td>
        <td>${escapeHtml(e.ip_address || '—')}</td>
        <td>${escapeHtml(e.reason || '—')}</td>
    </tr>`).join('');
}

function openCreateTokenModal() {
    const modal = document.getElementById('createTokenModal');
    if (modal) modal.style.display = 'flex';
}

function closeCreateTokenModal() {
    const modal = document.getElementById('createTokenModal');
    if (modal) modal.style.display = 'none';
    ['newTokenName', 'newTokenOwner', 'newTokenNotes', 'newTokenExpires'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const role = document.getElementById('newTokenRole');
    if (role) role.value = 'user';
}

async function submitCreateToken() {
    const adminToken = localStorage.getItem('adminToken');
    if (!adminToken) return;
    const name = document.getElementById('newTokenName')?.value.trim();
    if (!name) { showNotification('Name is required', 'error'); return; }
    const body = {
        name,
        owner: document.getElementById('newTokenOwner')?.value.trim() || null,
        role: document.getElementById('newTokenRole')?.value || 'user',
        notes: document.getElementById('newTokenNotes')?.value.trim() || null,
        expires_at: document.getElementById('newTokenExpires')?.value.trim() || null,
    };
    try {
        const res = await fetch(`${CONFIG.apiBaseUrl}/admin/tokens`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${adminToken}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        closeCreateTokenModal();
        showNewTokenModal(data);
        fetchTokens();
    } catch (e) {
        showNotification('Failed to create token: ' + e.message, 'error');
    }
}

function showNewTokenModal(tokenInfo) {
    const modal = document.getElementById('newTokenModal');
    const rawInput = document.getElementById('newTokenRaw');
    const details = document.getElementById('newTokenDetails');
    if (rawInput) rawInput.value = tokenInfo.raw_token || '';
    if (details) details.innerHTML = `
        <strong>Name:</strong> ${escapeHtml(tokenInfo.name)} &nbsp;
        <strong>Role:</strong> ${escapeHtml(tokenInfo.role)} &nbsp;
        <strong>Masked:</strong> <code>${escapeHtml(tokenInfo.token_masked)}</code>
    `;
    if (modal) modal.style.display = 'flex';
}

function closeNewTokenModal() {
    const modal = document.getElementById('newTokenModal');
    if (modal) modal.style.display = 'none';
    const raw = document.getElementById('newTokenRaw');
    if (raw) raw.value = '';
}

function copyNewToken() {
    const raw = document.getElementById('newTokenRaw');
    if (!raw) return;
    raw.select();
    document.execCommand('copy');
    showNotification('Token copied to clipboard!', 'success');
}

async function revokeToken(tokenId, name) {
    if (!confirm(`Revoke token "${name}"? This cannot be undone.`)) return;
    const adminToken = localStorage.getItem('adminToken');
    if (!adminToken) return;
    try {
        const res = await fetch(`${CONFIG.apiBaseUrl}/admin/tokens/${tokenId}/revoke`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${adminToken}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Revoked via dashboard' }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        showNotification(`Token "${name}" revoked`, 'success');
        fetchTokens();
        fetchTokenAudit();
    } catch (e) {
        showNotification('Failed to revoke: ' + e.message, 'error');
    }
}

async function rotateToken(tokenId, name) {
    if (!confirm(`Rotate token "${name}"? Old token will be revoked and a new one created.`)) return;
    const adminToken = localStorage.getItem('adminToken');
    if (!adminToken) return;
    try {
        const res = await fetch(`${CONFIG.apiBaseUrl}/admin/tokens/${tokenId}/rotate`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${adminToken}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Rotated via dashboard' }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        showNewTokenModal(data);
        fetchTokens();
        fetchTokenAudit();
    } catch (e) {
        showNotification('Failed to rotate: ' + e.message, 'error');
    }
}

// ─── Analytics ────────────────────────────────────────────────────────────────

async function fetchAnalytics() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;
    const hours = document.getElementById('analyticsHours')?.value || 24;
    try {
        const [failRes, sigRes, latRes, trendRes] = await Promise.all([
            fetch(`${CONFIG.apiBaseUrl}/admin/analytics/failures?hours=${hours}`, { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch(`${CONFIG.apiBaseUrl}/admin/analytics/signatures?hours=${hours}&limit=20`, { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch(`${CONFIG.apiBaseUrl}/admin/analytics/latency?hours=${hours}`, { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch(`${CONFIG.apiBaseUrl}/admin/analytics/trends?hours=${hours}`, { headers: { 'Authorization': `Bearer ${token}` } }),
        ]);
        if (failRes.ok) {
            const d = await failRes.json();
            renderAnalyticsSummary(d);
            renderFailuresByPath(d.by_path || []);
            renderFailuresByStatus(d.by_status_code || []);
        }
        if (sigRes.ok) {
            const d = await sigRes.json();
            renderTopSignatures(d.signatures || []);
        }
        if (latRes.ok) {
            const d = await latRes.json();
            renderLatencyStats(d.endpoints || []);
        }
        if (trendRes.ok) {
            const d = await trendRes.json();
            renderTrends(d.buckets || []);
        }
    } catch (e) {
        console.error('Analytics fetch error:', e);
        showNotification('Failed to load analytics: ' + e.message, 'error');
    }
}

function renderAnalyticsSummary(failData) {
    const container = document.getElementById('analyticsSummary');
    if (!container) return;
    const totalFails = (failData.by_status_code || []).reduce((s, r) => s + r.count, 0);
    const e4xx = (failData.by_status_code || []).filter(r => r.status >= 400 && r.status < 500).reduce((s, r) => s + r.count, 0);
    const e5xx = (failData.by_status_code || []).filter(r => r.status >= 500).reduce((s, r) => s + r.count, 0);
    const affectedPaths = (failData.by_path || []).length;
    container.innerHTML = [
        { label: 'Total Failures', value: totalFails, color: '#ef4444' },
        { label: '4xx Errors', value: e4xx, color: '#f59e0b' },
        { label: '5xx Errors', value: e5xx, color: '#dc2626' },
        { label: 'Affected Endpoints', value: affectedPaths, color: '#4f46e5' },
    ].map(c => `
        <div style="background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:16px; border-left:4px solid ${c.color};">
            <div style="font-size:0.8rem; color:#6b7280; margin-bottom:4px;">${c.label}</div>
            <div style="font-size:1.8rem; font-weight:700; color:${c.color};">${c.value}</div>
        </div>
    `).join('');
}

function renderFailuresByPath(rows) {
    const tbody = document.getElementById('failuresByPathBody');
    if (!tbody) return;
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="3" class="text-center">No failures</td></tr>'; return; }
    const max = rows[0].count || 1;
    tbody.innerHTML = rows.slice(0, 10).map(r => {
        const pct = Math.round((r.count / max) * 100);
        return `<tr>
            <td><code style="font-size:0.85em;">${escapeHtml(r.path)}</code></td>
            <td><strong>${r.count}</strong></td>
            <td><div style="background:#fee2e2; border-radius:4px; height:8px;">
                <div style="width:${pct}%; background:#ef4444; height:8px; border-radius:4px;"></div>
            </div></td>
        </tr>`;
    }).join('');
}

function renderFailuresByStatus(rows) {
    const tbody = document.getElementById('failuresByStatusBody');
    if (!tbody) return;
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="3" class="text-center">No failures</td></tr>'; return; }
    const max = Math.max(...rows.map(r => r.count)) || 1;
    const colors = { 400: '#f59e0b', 401: '#f59e0b', 403: '#f59e0b', 404: '#64748b', 429: '#f97316', 500: '#ef4444', 502: '#dc2626', 503: '#dc2626' };
    tbody.innerHTML = rows.map(r => {
        const pct = Math.round((r.count / max) * 100);
        const color = colors[r.status] || '#6b7280';
        return `<tr>
            <td><span style="padding:2px 8px; border-radius:4px; background:${color}; color:#fff; font-size:0.85em;">${r.status}</span></td>
            <td><strong>${r.count}</strong></td>
            <td><div style="background:#f3f4f6; border-radius:4px; height:8px;">
                <div style="width:${pct}%; background:${color}; height:8px; border-radius:4px;"></div>
            </div></td>
        </tr>`;
    }).join('');
}

function renderTopSignatures(sigs) {
    const tbody = document.getElementById('topSignaturesBody');
    if (!tbody) return;
    if (!sigs.length) { tbody.innerHTML = '<tr><td colspan="6" class="text-center">No data</td></tr>'; return; }
    tbody.innerHTML = sigs.map(s => {
        const params = typeof s.query_params === 'object' ? JSON.stringify(s.query_params) : String(s.query_params || '');
        return `<tr>
            <td><code style="font-size:0.85em;">${escapeHtml(s.path)}</code></td>
            <td><span style="padding:2px 6px; border-radius:4px; background:#fee2e2; color:#991b1b; font-size:0.85em;">${s.response_status}</span></td>
            <td style="font-size:0.85em; color:#64748b; max-width:200px; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(params)}</td>
            <td><strong>${s.count}</strong></td>
            <td><small>${formatTimestamp(s.first_seen)}</small></td>
            <td><small>${formatTimestamp(s.last_seen)}</small></td>
        </tr>`;
    }).join('');
}

function renderLatencyStats(endpoints) {
    const tbody = document.getElementById('latencyBody');
    if (!tbody) return;
    if (!endpoints.length) { tbody.innerHTML = '<tr><td colspan="8" class="text-center">No data</td></tr>'; return; }
    tbody.innerHTML = endpoints.map(e => `<tr>
        <td><code style="font-size:0.85em;">${escapeHtml(e.path)}</code></td>
        <td>${e.count}</td>
        <td>${e.min_ms}</td>
        <td>${e.avg_ms}</td>
        <td>${e.p50_ms}</td>
        <td style="${e.p95_ms > 500 ? 'color:#ef4444; font-weight:600;' : ''}">${e.p95_ms}</td>
        <td style="${e.p99_ms > 1000 ? 'color:#dc2626; font-weight:700;' : ''}">${e.p99_ms}</td>
        <td>${e.max_ms}</td>
    </tr>`).join('');
}

function renderTrends(buckets) {
    const container = document.getElementById('trendsChart');
    if (!container) return;
    if (!buckets.length) {
        container.innerHTML = '<p style="text-align:center; color:#9ca3af; padding:20px 0;">No data for selected period</p>';
        return;
    }
    const maxTotal = Math.max(...buckets.map(b => b.total)) || 1;
    container.innerHTML = `
        <div style="display:flex; align-items:flex-end; gap:4px; height:100px; overflow-x:auto; padding-bottom:4px;">
            ${buckets.map(b => {
                const hTotal = Math.max(2, Math.round((b.total / maxTotal) * 90));
                const hErr = b.total > 0 ? Math.round((b.errors / b.total) * hTotal) : 0;
                const label = b.bucket ? b.bucket.substring(11, 16) : '';
                return `<div style="display:flex; flex-direction:column; align-items:center; flex:1; min-width:28px; gap:2px;" title="${escapeHtml(b.bucket)}: ${b.total} total, ${b.errors} errors">
                    <div style="width:100%; display:flex; flex-direction:column; justify-content:flex-end; height:90px;">
                        <div style="background:#ef4444; height:${hErr}px; border-radius:2px 2px 0 0;"></div>
                        <div style="background:#4f46e5; height:${hTotal - hErr}px;"></div>
                    </div>
                    <div style="font-size:0.65rem; color:#9ca3af; white-space:nowrap;">${escapeHtml(label)}</div>
                </div>`;
            }).join('')}
        </div>
        <div style="display:flex; gap:16px; margin-top:8px; font-size:0.8rem; color:#6b7280;">
            <span><span style="display:inline-block; width:10px; height:10px; background:#4f46e5; border-radius:2px; margin-right:4px;"></span>Success</span>
            <span><span style="display:inline-block; width:10px; height:10px; background:#ef4444; border-radius:2px; margin-right:4px;"></span>Errors</span>
        </div>
    `;
}

function exportAnalytics() {
    const token = localStorage.getItem('adminToken');
    if (!token) return;
    const hours = document.getElementById('analyticsHours')?.value || 24;
    fetch(`${CONFIG.apiBaseUrl}/admin/analytics/failures?hours=${hours}`, { headers: { 'Authorization': `Bearer ${token}` } })
        .then(r => r.json())
        .then(data => {
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `analytics_failures_${hours}h_${new Date().toISOString().slice(0,10)}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
        })
        .catch(e => showNotification('Export failed: ' + e.message, 'error'));
}
