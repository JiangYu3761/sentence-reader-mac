package com.click.shell;

import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.Key;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class AndroidCredentialStore {
    private static final String ANDROID_KEYSTORE = "AndroidKeyStore";
    private static final String KEY_ALIAS = "com.click.shell.credentials.aes_gcm.v1";
    private static final String TRANSFORMATION = "AES/GCM/NoPadding";
    private static final String ENVELOPE_VERSION = "v1";
    private static final String PREF_ACCESS_TOKEN = "secure_access_token_v1";
    private static final String PREF_PAIRING_SECRET = "secure_pairing_secret_v1";

    private final SharedPreferences preferences;

    AndroidCredentialStore(SharedPreferences preferences) {
        this.preferences = preferences;
    }

    synchronized void migrateLegacyAccessToken(String legacyPreferenceKey) {
        String legacyToken = clean(preferences.getString(legacyPreferenceKey, ""));
        if (legacyToken.isEmpty()) {
            commitOrThrow(preferences.edit().remove(legacyPreferenceKey), "Cannot remove empty legacy credential");
            return;
        }
        writeEncrypted(PREF_ACCESS_TOKEN, legacyToken);
        if (!legacyToken.equals(readEncrypted(PREF_ACCESS_TOKEN))) {
            throw new IllegalStateException("Cannot verify migrated Android credential");
        }
        commitOrThrow(preferences.edit().remove(legacyPreferenceKey), "Cannot remove legacy Android credential");
    }

    synchronized String accessToken() {
        return readEncrypted(PREF_ACCESS_TOKEN);
    }

    synchronized String accessTokenReadOnly() {
        return readEncryptedReadOnly(PREF_ACCESS_TOKEN);
    }

    synchronized void saveAccessToken(String accessToken) {
        writeEncrypted(PREF_ACCESS_TOKEN, clean(accessToken));
    }

    synchronized String pairingSecret() {
        return readEncrypted(PREF_PAIRING_SECRET);
    }

    synchronized void savePairingSecret(String pairingSecret) {
        writeEncrypted(PREF_PAIRING_SECRET, clean(pairingSecret));
    }

    synchronized void clearPairingSecret() {
        commitOrThrow(preferences.edit().remove(PREF_PAIRING_SECRET), "Cannot clear pairing secret");
    }

    private void writeEncrypted(String preferenceKey, String plaintext) {
        if (plaintext.isEmpty()) {
            commitOrThrow(preferences.edit().remove(preferenceKey), "Cannot clear encrypted credential");
            return;
        }
        try {
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.ENCRYPT_MODE, secretKey());
            cipher.updateAAD(preferenceKey.getBytes(StandardCharsets.UTF_8));
            byte[] ciphertext = cipher.doFinal(plaintext.getBytes(StandardCharsets.UTF_8));
            String envelope = ENVELOPE_VERSION
                    + ":"
                    + Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
                    + ":"
                    + Base64.encodeToString(ciphertext, Base64.NO_WRAP);
            commitOrThrow(preferences.edit().putString(preferenceKey, envelope), "Cannot persist encrypted credential");
        } catch (Throwable throwable) {
            throw new IllegalStateException("Android Keystore encryption failed", throwable);
        }
    }

    private String readEncrypted(String preferenceKey) {
        String envelope = clean(preferences.getString(preferenceKey, ""));
        if (envelope.isEmpty()) {
            return "";
        }
        try {
            String[] parts = envelope.split(":", 3);
            if (parts.length != 3 || !ENVELOPE_VERSION.equals(parts[0])) {
                throw new IllegalStateException("Unsupported encrypted credential envelope");
            }
            byte[] iv = Base64.decode(parts[1], Base64.NO_WRAP);
            byte[] ciphertext = Base64.decode(parts[2], Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(128, iv));
            cipher.updateAAD(preferenceKey.getBytes(StandardCharsets.UTF_8));
            return clean(new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            preferences.edit().remove(preferenceKey).commit();
            return "";
        }
    }

    private String readEncryptedReadOnly(String preferenceKey) {
        String envelope = clean(preferences.getString(preferenceKey, ""));
        if (envelope.isEmpty()) {
            return "";
        }
        try {
            String[] parts = envelope.split(":", 3);
            if (parts.length != 3 || !ENVELOPE_VERSION.equals(parts[0])) {
                return "";
            }
            SecretKey key = existingSecretKey();
            if (key == null) {
                return "";
            }
            byte[] iv = Base64.decode(parts[1], Base64.NO_WRAP);
            byte[] ciphertext = Base64.decode(parts[2], Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, iv));
            cipher.updateAAD(preferenceKey.getBytes(StandardCharsets.UTF_8));
            return clean(new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return "";
        }
    }

    private SecretKey existingSecretKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        Key existing = keyStore.getKey(KEY_ALIAS, null);
        return existing instanceof SecretKey ? (SecretKey) existing : null;
    }

    private SecretKey secretKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        Key existing = keyStore.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) {
            return (SecretKey) existing;
        }
        if (existing != null) {
            throw new IllegalStateException("Android credential key alias has an unexpected type");
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE);
        generator.init(
                new KeyGenParameterSpec.Builder(
                        KEY_ALIAS,
                        KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
                )
                        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                        .setRandomizedEncryptionRequired(true)
                        .build()
        );
        return generator.generateKey();
    }

    private void commitOrThrow(SharedPreferences.Editor editor, String message) {
        if (!editor.commit()) {
            throw new IllegalStateException(message);
        }
    }

    private String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
