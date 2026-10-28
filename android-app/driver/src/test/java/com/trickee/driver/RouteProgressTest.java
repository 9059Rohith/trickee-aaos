package com.trickee.driver;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class RouteProgressTest {
    private RouteProgress route() {
        return new RouteProgress(new double[][] {{77.0, 12.0}, {77.01, 12.0}, {77.02, 12.0}},
                new RouteProgress.Step[] {
                        new RouteProgress.Step(12.0, 77.01, "turn", "right", "Main Road"),
                        new RouteProgress.Step(12.0, 77.02, "arrive", "", "Destination")
                });
    }

    @Test public void advancesTurnsAndRemainingDistanceWithoutMovingBackward() {
        RouteProgress progress = route();
        long now = 1_000_000;
        RouteProgress.State start = progress.update(12.0, 77.0, 5, now, now, false);
        assertEquals(RouteProgress.Status.ON_ROUTE, start.status);
        assertEquals(0, start.nextStepIndex);
        RouteProgress.State pastTurn = progress.update(12.0, 77.012, 5, now + 60000, now + 60000, false);
        assertEquals(1, pastTurn.nextStepIndex);
        assertTrue(pastTurn.remainingMeters < start.remainingMeters);
        RouteProgress.State backwardFix = progress.update(12.0, 77.005, 5, now + 65000, now + 65000, false);
        assertTrue(backwardFix.progressMeters >= pastTurn.progressMeters);
    }

    @Test public void ignoresMockedStaleAndInaccurateLocations() {
        RouteProgress progress = route();
        long now = 1_000_000;
        RouteProgress.State start = progress.update(12.0, 77.0, 5, now, now, false);
        for (RouteProgress.State invalid : new RouteProgress.State[] {
                progress.update(12.0, 77.015, 5, now + 1000, now + 1000, true),
                progress.update(12.0, 77.015, 5, now - 30000, now + 2000, false),
                progress.update(12.0, 77.015, 150, now + 3000, now + 3000, false),
        }) {
            assertEquals(RouteProgress.Status.LOCATION_UNAVAILABLE, invalid.status);
            assertEquals(start.progressMeters, invalid.progressMeters, 0.01);
            assertFalse(invalid.rerouteDue);
        }
    }

    @Test public void repeatedOffRouteFixesTriggerOneBoundedReroute() {
        RouteProgress progress = route();
        long now = 1_000_000;
        progress.update(12.0, 77.0, 5, now, now, false);
        assertFalse(progress.update(12.02, 77.005, 5, now + 1000, now + 1000, false).rerouteDue);
        assertFalse(progress.update(12.02, 77.006, 5, now + 2000, now + 2000, false).rerouteDue);
        RouteProgress.State third = progress.update(12.02, 77.007, 5, now + 3000, now + 3000, false);
        assertEquals(RouteProgress.Status.OFF_ROUTE, third.status);
        assertTrue(third.rerouteDue);
        assertFalse(progress.update(12.02, 77.008, 5, now + 4000, now + 4000, false).rerouteDue);
        assertTrue(progress.update(12.02, 77.009, 5, now + 34000, now + 34000, false).rerouteDue);
    }

    @Test public void routeCrossingDoesNotJumpFromStartToLaterLoop() {
        RouteProgress progress = new RouteProgress(new double[][] {
                {77.0, 12.0}, {77.01, 12.0}, {77.01, 12.01}, {77.0, 12.01},
                {77.0, 12.0}, {77.02, 12.0}
        }, new RouteProgress.Step[] {new RouteProgress.Step(12.0, 77.02, "arrive", "", "End")});
        long now = 1_000_000;
        RouteProgress.State start = progress.update(12.0, 77.0, 5, now, now, false);
        assertTrue(start.progressMeters < 50);
        assertTrue(start.remainingMeters > 4000);
    }

    @Test public void aCloserLaterLoopSegmentCannotStealTheFirstFix() {
        RouteProgress progress = new RouteProgress(new double[][] {
                {77.0, 12.0}, {77.01, 12.0}, {77.01, 12.01}, {77.0, 12.01},
                {77.0, 12.0001}, {77.02, 12.0001}
        }, new RouteProgress.Step[] {new RouteProgress.Step(12.0001, 77.02, "arrive", "", "End")});
        long now = 1_000_000;
        RouteProgress.State first = progress.update(12.0001, 77.0, 5, now, now, false);
        assertEquals(RouteProgress.Status.ON_ROUTE, first.status);
        assertTrue(first.progressMeters < 100);
    }

    @Test public void implausibleDestinationJumpDoesNotAnnounceArrival() {
        RouteProgress progress = route();
        long now = 1_000_000;
        progress.update(12.0, 77.0, 5, now, now, false);
        RouteProgress.State jump = progress.update(12.0, 77.02, 5, now + 1000, now + 1000, false);
        assertEquals(RouteProgress.Status.LOCATION_UNAVAILABLE, jump.status);
        assertTrue(jump.remainingMeters > 2000);
    }

    @Test public void arrivalNeedsProgressNearEndAndDestinationFix() {
        RouteProgress progress = route();
        long now = 1_000_000;
        progress.update(12.0, 77.0, 5, now, now, false);
        RouteProgress.State end = progress.update(12.0, 77.0199, 5, now + 120000, now + 120000, false);
        assertEquals(RouteProgress.Status.ARRIVED, end.status);
        assertTrue(end.remainingMeters <= 35);
    }
}
