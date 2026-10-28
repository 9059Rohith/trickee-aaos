package com.trickee.driver;

import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.Arrays;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Seals the driver bearer credential with a non-exportable Android Keystore key. */
final class SecretStore {
    private static final String ALIAS = "trickee.driver.credential.v1";
    private static final String SEALED = "driver_credential_sealed";
    private static final int IV_BYTES = 12;

    private SecretStore() { }

    static void save(SharedPreferences settings, String value) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = cipher.getIV();
        byte[] ciphertext = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        byte[] sealed = new byte[iv.length + ciphertext.length];
        System.arraycopy(iv, 0, sealed, 0, iv.length);
        System.arraycopy(ciphertext, 0, sealed, iv.length, ciphertext.length);
        if (!settings.edit().putString(SEALED, Base64.encodeToString(sealed, Base64.NO_WRAP)).commit())
            throw new IllegalStateException("credential storage failed");
    }

    static String read(SharedPreferences settings) throws Exception {
        String encoded = settings.getString(SEALED, "");
        if (encoded.isEmpty()) return "";
        byte[] sealed = Base64.decode(encoded, Base64.NO_WRAP);
        if (sealed.length < IV_BYTES + 16) throw new IllegalStateException("invalid sealed credential");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Arrays.copyOfRange(sealed, 0, IV_BYTES)));
        return new String(cipher.doFinal(sealed, IV_BYTES, sealed.length - IV_BYTES), StandardCharsets.UTF_8);
    }

    static void clear(SharedPreferences settings) {
        if (!settings.edit().remove(SEALED).commit()) throw new IllegalStateException("credential removal failed");
    }

    private static SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        SecretKey existing = (SecretKey) store.getKey(ALIAS, null);
        if (existing != null) return existing;
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build());
        return generator.generateKey();
    }
}
