package com.aitrading.app.features.dashboard

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Placeholder for the Home dashboard (blueprint §93): market status, top
 * opportunities from the scanner, portfolio summary, auto-trading toggle.
 * Wire this to [DashboardViewModel] once the backend endpoints it calls
 * (`/scanner`, `/portfolio`) are reachable from a real deployment.
 */
@Composable
fun DashboardScreen() {
    Column(modifier = Modifier.padding(16.dp)) {
        Text("AI Trading Platform")
        Text("Backend not connected — this is a UI scaffold.")
    }
}
