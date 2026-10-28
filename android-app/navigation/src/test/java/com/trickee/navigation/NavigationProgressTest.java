package com.trickee.navigation;

import org.junit.Test;

import static org.junit.Assert.*;

public class NavigationProgressTest {
    private static final String ROUTE = "{\"code\":\"Ok\",\"routes\":[{\"distance\":1600," +
            "\"duration\":240,\"geometry\":{\"type\":\"LineString\",\"coordinates\":[" +
            "[77.5900,12.9700],[77.5950,12.9750],[77.6000,12.9800]]}," +
            "\"legs\":[{\"steps\":[" +
            "{\"name\":\"Hosur Road\",\"maneuver\":{\"type\":\"turn\",\"modifier\":\"right\",\"location\":[77.5950,12.9750]}}," +
            "{\"name\":\"\",\"maneuver\":{\"type\":\"arrive\",\"location\":[77.6000,12.9800]}}]}]}]}";

    private NavigationProgress progress() throws Exception {
        NavigationRoute route = NavigationRoute.parse(ROUTE,
                new NavigationRoute.Point(12.9700, 77.5900),
                new NavigationRoute.Point(12.9800, 77.6000));
        return new NavigationProgress(route);
    }

    @Test public void advancesTurnsAndArrivesOnlyAtEnd() throws Exception {
        NavigationProgress engine = progress();
        NavigationProgress.State start = engine.update(12.9700, 77.5900, 8, 1_000_000, 1_000_000, false);
        assertEquals(NavigationProgress.Status.ON_ROUTE, start.status);
        assertEquals(0, start.nextTurnIndex);
        NavigationProgress.State middle = engine.update(12.9752, 77.5952, 8, 1_030_000, 1_030_000, false);
        assertEquals(NavigationProgress.Status.ON_ROUTE, middle.status);
        assertEquals(1, middle.nextTurnIndex);
        assertTrue(middle.remainingMetres < start.remainingMetres);
        NavigationProgress.State end = engine.update(12.9800, 77.6000, 8, 1_060_000, 1_060_000, false);
        assertEquals(NavigationProgress.Status.ARRIVED, end.status);
        assertEquals(-1, end.nextTurnIndex);
    }

    @Test public void rejectsUntrustedFixWithoutMovingProgress() throws Exception {
        NavigationProgress engine = progress();
        NavigationProgress.State start = engine.update(12.9700, 77.5900, 8, 1_000_000, 1_000_000, false);
        assertEquals(NavigationProgress.Status.LOCATION_UNAVAILABLE,
                engine.update(12.9750, 77.5950, 8, 980_000, 1_000_000, false).status);
        assertEquals(NavigationProgress.Status.LOCATION_UNAVAILABLE,
                engine.update(12.9750, 77.5950, 150, 1_001_000, 1_001_000, false).status);
        assertEquals(NavigationProgress.Status.LOCATION_UNAVAILABLE,
                engine.update(12.9750, 77.5950, 8, 1_002_000, 1_002_000, true).status);
        assertEquals(start.remainingMetres,
                engine.update(12.9700, 77.5900, 8, 1_003_000, 1_003_000, false).remainingMetres, 1);
    }

    @Test public void reroutesOnlyAfterRepeatedOffRouteFixesAndCooldown() throws Exception {
        NavigationProgress engine = progress();
        engine.update(12.9700, 77.5900, 8, 1_000_000, 1_000_000, false);
        assertFalse(engine.update(12.9710, 77.6000, 8, 1_001_000, 1_001_000, false).rerouteDue);
        assertFalse(engine.update(12.9710, 77.6000, 8, 1_002_000, 1_002_000, false).rerouteDue);
        assertTrue(engine.update(12.9710, 77.6000, 8, 1_003_000, 1_003_000, false).rerouteDue);
        assertFalse(engine.update(12.9710, 77.6000, 8, 1_004_000, 1_004_000, false).rerouteDue);
        assertTrue(engine.update(12.9710, 77.6000, 8, 1_034_000, 1_034_000, false).rerouteDue);
    }

    @Test public void rejectsImpossibleJumpAndDoesNotAdvanceTurn() throws Exception {
        NavigationProgress engine = progress();
        NavigationProgress.State start = engine.update(12.9700, 77.5900, 8,
                1_000_000, 1_000_000, false);
        NavigationProgress.State jump = engine.update(12.9750, 77.5950, 8,
                1_001_000, 1_001_000, false);
        assertEquals(NavigationProgress.Status.LOCATION_UNAVAILABLE, jump.status);
        assertEquals(0, jump.nextTurnIndex);
        assertEquals(start.remainingMetres, jump.remainingMetres, 1);
        assertEquals(NavigationProgress.Status.ON_ROUTE,
                engine.update(12.9700, 77.5900, 8, 1_002_000, 1_002_000, false).status);
    }

    @Test public void replacementRouteStartsWithIndependentProgress() throws Exception {
        NavigationProgress original = progress();
        original.update(12.9700, 77.5900, 8, 1_000_000, 1_000_000, false);
        assertEquals(1, original.update(12.9752, 77.5952, 8,
                1_030_000, 1_030_000, false).nextTurnIndex);
        NavigationProgress replacement = progress();
        NavigationProgress.State start = replacement.update(12.9700, 77.5900, 8,
                1_031_000, 1_031_000, false);
        assertEquals(0, start.nextTurnIndex);
        assertEquals(NavigationProgress.Status.ON_ROUTE, start.status);
    }
}
