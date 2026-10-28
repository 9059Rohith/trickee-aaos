package com.trickee.driver;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;

public final class DriverApiOriginTest {
    @Test public void acceptsOnlyBareSecureOriginForRestorableDraft() {
        assertEquals("https://fleet.example", DriverApi.validateOrigin(" https://fleet.example/ "));
    }

    @Test public void rejectsEmbeddedCredentialsAndRequestParameters() {
        for (String value : new String[] {
                "https://driver:secret@fleet.example", "https://fleet.example/?token=secret",
                "https://fleet.example/api/private", "https://fleet.example/#secret",
                "http://fleet.example"}) {
            assertThrows(value, IllegalArgumentException.class, () -> DriverApi.validateOrigin(value));
        }
    }
}
