package com.trickee.navigation;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

public class GuidanceNoticeTest {
    private static final String ROUTE = "{\"code\":\"Ok\",\"routes\":[{\"distance\":1600," +
            "\"duration\":240,\"geometry\":{\"type\":\"LineString\",\"coordinates\":[" +
            "[77.5900,12.9700],[77.5950,12.9750],[77.6000,12.9800]]}," +
            "\"legs\":[{\"steps\":[{\"name\":\"Hosur Road\",\"maneuver\":{" +
            "\"type\":\"turn\",\"modifier\":\"right\",\"location\":[77.5950,12.9750]}}]}]}]}";

    private NavigationRoute route() throws Exception {
        return NavigationRoute.parse(ROUTE, new NavigationRoute.Point(12.9700, 77.5900),
                new NavigationRoute.Point(12.9800, 77.6000));
    }

    @Test public void givesCurrentTurnAndDistance() throws Exception {
        assertEquals("In 250 m, turn right onto Hosur Road", GuidanceNotice.content(route(),
                new NavigationProgress.State(NavigationProgress.Status.ON_ROUTE,
                        1200, 247, 0, false)));
        assertEquals("In 1.2 km, turn right onto Hosur Road", GuidanceNotice.content(route(),
                new NavigationProgress.State(NavigationProgress.Status.ON_ROUTE,
                        1600, 1200, 0, false)));
    }

    @Test public void pausesRatherThanClaimingAValidTurn() throws Exception {
        NavigationRoute route = route();
        assertEquals("Car location unavailable. Guidance paused.", GuidanceNotice.content(route,
                new NavigationProgress.State(NavigationProgress.Status.LOCATION_UNAVAILABLE,
                        1200, 247, 0, false)));
        assertEquals("Off route. Waiting for a safe road route.", GuidanceNotice.content(route,
                new NavigationProgress.State(NavigationProgress.Status.OFF_ROUTE,
                        1200, 247, 0, false)));
    }
}
