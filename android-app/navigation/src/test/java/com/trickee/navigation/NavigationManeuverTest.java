package com.trickee.navigation;

import androidx.car.app.navigation.model.Maneuver;

import org.junit.Test;

import static org.junit.Assert.*;

public class NavigationManeuverTest {
    private NavigationRoute.Turn turn(String type, String modifier) {
        return new NavigationRoute.Turn(new NavigationRoute.Point(12.9, 77.6),
                100, type + " " + modifier, type, modifier, "Test Road");
    }

    @Test public void mapsSupportedProviderDirectionsWithoutInventingOne() {
        assertEquals(Maneuver.TYPE_STRAIGHT,
                NavigationManeuver.carType(turn("continue", "straight")));
        assertEquals(Maneuver.TYPE_TURN_NORMAL_RIGHT,
                NavigationManeuver.carType(turn("turn", "right")));
        assertEquals(Maneuver.TYPE_TURN_SHARP_LEFT,
                NavigationManeuver.carType(turn("turn", "sharp left")));
        assertEquals(Maneuver.TYPE_DESTINATION,
                NavigationManeuver.carType(turn("arrive", "")));
        assertEquals(Maneuver.TYPE_UNKNOWN,
                NavigationManeuver.carType(turn("roundabout", "")));
    }
}
