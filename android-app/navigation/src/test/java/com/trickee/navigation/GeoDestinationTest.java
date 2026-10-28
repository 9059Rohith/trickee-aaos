package com.trickee.navigation;

import org.junit.Test;

import static org.junit.Assert.*;

public class GeoDestinationTest {
    @Test public void acceptsCoordinateIntents() {
        GeoDestination plain = GeoDestination.parse("geo:12.9716,77.5946");
        assertNotNull(plain);
        assertEquals(12.9716, plain.latitude, 0.000001);
        assertEquals(77.5946, plain.longitude, 0.000001);

        GeoDestination labeled = GeoDestination.parse("geo:0,0?q=12.7409,77.8253%28Office%29");
        assertNotNull(labeled);
        assertEquals("Office", labeled.label);
        assertEquals(12.7409, labeled.latitude, 0.000001);
    }

    @Test public void rejectsSearchOnlyAndMalformedValues() {
        assertNull(GeoDestination.parse("geo:0,0?q=coffee"));
        assertNull(GeoDestination.parse("geo:91,0"));
        assertNull(GeoDestination.parse("geo:0,181"));
        assertNull(GeoDestination.parse("geo:NaN,77"));
        assertNull(GeoDestination.parse("geo:1e309,77"));
        assertNull(GeoDestination.parse("https://example.com/?q=12,77"));
        assertNull(GeoDestination.parse("geo:12,77,6"));
        assertNull(GeoDestination.parse("geo:12,77?q=1,2%0AInjected"));
    }

    @Test public void boundsLabelAndKeepsCoordinatesExact() {
        GeoDestination destination = GeoDestination.parse("geo:0,0?q=-90,-180%28A%20safe%20name%29");
        assertNotNull(destination);
        assertEquals(-90.0, destination.latitude, 0.0);
        assertEquals(-180.0, destination.longitude, 0.0);
        assertEquals("A safe name", destination.label);
        assertNull(GeoDestination.parse("geo:0,0?q=1,2%28" + "a".repeat(65) + "%29"));
    }
}
