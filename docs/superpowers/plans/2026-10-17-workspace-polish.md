# Workspace polish and verification

User intent: preserve Trickee's brand and real operational workflows, improve the entire usable browser workspace, proceed without questions. Existing native runtime and production evidence gaps remain explicit.

Design: extend the existing code-native UI rather than redesign imagery. Both browser clients gain an accessible, searchable section navigator using native dialog, keyboard shortcut, real anchors and focus restoration. Shared motion tokens and a persisted non-sensitive motion preference provide consistent feedback with operating-system reduced motion taking precedence. Fleet gains actual vehicle search and state filtering with truthful empty states. No decorative particle, 3D, cursor or scroll-jacking effects in operational driving software.

Implementation sequence:
1. Add browser acceptance checks for navigator, preferences, keyboard, filters and responsive layouts; establish failing baseline.
2. Implement shared assets and both clients, include new assets in offline shell.
3. Exercise existing operational suites plus new checks, inspect screenshots, review and fix defects.
4. Record measured evidence, catalogue animation choices and remaining release blockers. No unsupported top-percentile or completion claims.

Verification matrix: Chromium, Firefox and WebKit when installed; widths 320, 375, 390, 430, 768, 1024, 1280, 1440, 1920; keyboard and mobile touch; OS reduced motion, user motion preference, dark/light fleet theme, disconnected and connected fleet states. Existing driver suite covers credentials, consent, trips, offline queues and service workflows.
