package com.click.shell;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class ClickPreflightAuthorizationPolicyTest {
    private static final String DEVICE_ID = "android-fixture";

    @Test
    public void acceptsOnlyStrictTokenAuthorization() {
        assertTrue(accepts(true, true, "authorized", true, DEVICE_ID));
    }

    @Test
    public void rejectsLocalLanAllowanceWithoutTokenProof() {
        assertFalse(accepts(true, true, "local_lan_allowed", false, DEVICE_ID));
    }

    @Test
    public void rejectsAuthorizedResponseWhenTokenIsNotRequired() {
        assertFalse(accepts(true, true, "authorized", false, DEVICE_ID));
    }

    @Test
    public void rejectsWrongToken() {
        assertFalse(accepts(true, false, "unknown", true, DEVICE_ID));
    }

    @Test
    public void rejectsRevokedDevice() {
        assertFalse(accepts(true, false, "unknown", false, DEVICE_ID));
    }

    @Test
    public void rejectsDifferentDeviceIdentity() {
        assertFalse(accepts(true, true, "authorized", true, "android-other"));
    }

    private static boolean accepts(
            boolean ok,
            boolean authorized,
            String status,
            boolean tokenRequired,
            String responseDeviceId
    ) {
        return MainActivity.isStrictPreflightAuthorization(
                200,
                ok,
                authorized,
                status,
                tokenRequired,
                DEVICE_ID,
                responseDeviceId
        );
    }
}
