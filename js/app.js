/**
 * Cache API Dashboard Application
 */

// Configuration
const CONFIG = {
    // Use the current page's origin so the dashboard works both locally
    // (http://localhost:5000) and in production without hardcoding a URL.
    apiBaseUrl: window.location.origin,
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
