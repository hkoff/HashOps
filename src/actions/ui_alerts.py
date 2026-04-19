# src/actions/ui_alerts.py — Backend interface for System Health Monitoring

# This module provides high-level helpers to push persistent or transient warning banners to the UI. 
# It manages a persistent in-memory state to ensure that critical alerts (like RPC outages or Gas safety triggers) are synchronized when the dashboard loads.

import threading
from typing import Dict, Any
from src.web_ui.sse import _broadcast

# ── STATE MANAGEMENT ──────────────────────────────────────────────────────────
# Stores active alerts: { alert_id: alert_payload }
_active_alerts: Dict[str, Dict[str, Any]] = {}
_alerts_lock = threading.Lock()

def push_system_alert(alert_id: str, title: str, message: str, 
                       alert_type: str = "warning", 
                       section: str = "global", 
                       persistent: bool = True):
    """
    Broadcasts a system alert to all connected UI clients and persists it in memory.
    
    Args:
        alert_id: Unique string identifying the alert (e.g. 'rpc-outage')
        title: Short bold headline for the banner
        message: Detailed body text explaining the issue or action required
        alert_type: 'warning', 'error', 'info', 'success'
        section: 'global' (top tray) or 'inventory' (panel specific)
        persistent: If True, the banner persists until explicitly removed by code
    """
    payload = {
        "type": "system_alert",
        "id": alert_id,
        "alert_type": alert_type,
        "title": title,
        "message": message,
        "section": section,
        "persistent": persistent
    }
    
    with _alerts_lock:
        _active_alerts[alert_id] = payload
        
    _broadcast(payload)

def remove_system_alert(alert_id: str):
    """
    Tells all UI clients to remove a specific alert and prunes it from the backend state.
    """
    with _alerts_lock:
        if alert_id in _active_alerts:
            del _active_alerts[alert_id]
            
    _broadcast({
        "type": "remove_system_alert",
        "id": alert_id
    })

def get_active_alerts() -> Dict[str, Dict[str, Any]]:
    """
    Returns a copy of all currently active system-wide alerts.
    Used by the dashboard initialization API to sync state.
    """
    with _alerts_lock:
        return dict(_active_alerts)
