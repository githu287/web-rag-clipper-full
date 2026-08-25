// Web RAG Clipper 统一配置
// 所有 JS 只从这里读取后端地址，禁止在其他文件中散落硬编码。
// 如需指向其他后端（如局域网 / 公网部署），只需修改 API_BASE_URL，
// 并同步 manifest.json 中 host_permissions 的对应地址。
const WEB_RAG_CLIPPER_CONFIG = {
  API_BASE_URL: "http://localhost:8000"
};
