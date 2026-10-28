package com.trickee.navigation;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class CarLocationTest {
    @Test public void requiresGuidanceFreshnessAndUsableAccuracy() {
        long now = 1_000_000;
        assertTrue(CarLocation.validFixMetadata(now - 15_000, now, 8));
        assertFalse(CarLocation.validFixMetadata(now - 15_001, now, 8));
        assertTrue(CarLocation.validFixMetadata(now + 5_000, now, 8));
        assertFalse(CarLocation.validFixMetadata(now + 5_001, now, 8));
        assertFalse(CarLocation.validFixMetadata(now, now, 0));
        assertFalse(CarLocation.validFixMetadata(now, now, Float.NaN));
        assertFalse(CarLocation.validFixMetadata(now, now, 101));
    }
}
