"use strict";

// Portable login identity only. Models, documents and chat drafts never enter this file.
const webRagWorkspaceBackup = (() => {
  const iterations = 600000, encoder = new TextEncoder();
  const aad = encoder.encode("web-rag-workspace-login:1");
  const base64 = bytes => btoa(String.fromCharCode(...bytes));
  function bytes(value, length) {
    if (typeof value !== "string" || value.length > 24000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new Error("invalid encoding");
    const result = Uint8Array.from(atob(value), ch => ch.charCodeAt(0));
    if (length && result.length !== length) throw new Error("invalid length");
    return result;
  }
  async function key(password, salt) {
    if (typeof password !== "string" || password.length < 10 || password.length > 256) throw new Error("登录文件密码需为10至256个字符，请妥善保存");
    const material = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", iterations, salt }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }
  async function encode(value, password) {
    const identity = webRagApiClient.connection(value);
    const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
    const encrypted = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: aad }, await key(password, salt), encoder.encode(JSON.stringify(identity)));
    return JSON.stringify({ type: "web-rag-workspace-login", version: 1, kdf: "PBKDF2-SHA256", iterations,
      salt: base64(salt), iv: base64(iv), ciphertext: base64(new Uint8Array(encrypted)) }, null, 2);
  }
  async function decode(text, password) {
    if (typeof text !== "string" || text.length > 32768) throw new Error("请选择不超过32KB的登录文件");
    try {
      const envelope = JSON.parse(text);
      if (envelope.type !== "web-rag-workspace-login" || envelope.version !== 1 || envelope.kdf !== "PBKDF2-SHA256" || envelope.iterations !== iterations) throw new Error("unsupported format");
      const salt = bytes(envelope.salt, 16), iv = bytes(envelope.iv, 12), ciphertext = bytes(envelope.ciphertext);
      if (ciphertext.length < 17 || ciphertext.length > 16384) throw new Error("invalid payload");
      const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv, additionalData: aad }, await key(password, salt), ciphertext);
      return webRagApiClient.connection(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(plaintext)));
    } catch (_error) { throw new Error("登录文件或密码不正确，或文件版本不受支持；未更改当前连接"); }
  }
  return { encode, decode };
})();
