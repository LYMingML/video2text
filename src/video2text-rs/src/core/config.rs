use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub workspace_dir: String,
    pub default_backend: String,
    pub default_funasr_model: String,
    pub default_whisper_model: String,
    pub auto_subtitle_lang: String,
    pub model_profiles: Vec<ModelProfile>,
    pub active_profile: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelProfile {
    pub name: String,
    pub base_url: String,
    pub api_key: String,
    pub default_model: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            host: "0.0.0.0".to_string(),
            port: 7881,
            workspace_dir: "workspace".to_string(),
            default_backend: "FunASR（Paraformer）".to_string(),
            default_funasr_model: "paraformer-zh ⭐ 普通话精度推荐".to_string(),
            default_whisper_model: "medium".to_string(),
            auto_subtitle_lang: "zh".to_string(),
            model_profiles: vec![ModelProfile {
                name: "default".to_string(),
                base_url: "https://api.siliconflow.cn/v1".to_string(),
                api_key: "".to_string(),
                default_model: "Pro/moonshotai/Kimi-K2.5".to_string(),
            }],
            active_profile: "default".to_string(),
        }
    }
}

impl Config {
    pub fn load() -> Result<Self> {
        // 尝试从 .env 文件加载
        if let Ok(path) = std::env::var("VIDEO2TEXT_CONFIG") {
            if std::path::Path::new(&path).exists() {
                let content = std::fs::read_to_string(&path)?;
                return Self::from_env_content(&content);
            }
        }

        if std::path::Path::new(".env").exists() {
            let content = std::fs::read_to_string(".env")?;
            return Self::from_env_content(&content);
        }

        // 从环境变量加载
        let mut config = Self::default();

        if let Ok(host) = std::env::var("HOST") {
            config.host = host;
        }
        if let Ok(port) = std::env::var("PORT") {
            if let Ok(p) = port.parse() {
                config.port = p;
            }
        }
        if let Ok(ws) = std::env::var("WORKSPACE_DIR") {
            config.workspace_dir = ws;
        }
        if let Ok(backend) = std::env::var("DEFAULT_BACKEND") {
            config.default_backend = backend;
        }
        if let Ok(model) = std::env::var("DEFAULT_FUNASR_MODEL") {
            config.default_funasr_model = model;
        }
        if let Ok(model) = std::env::var("DEFAULT_WHISPER_MODEL") {
            config.default_whisper_model = model;
        }
        if let Ok(lang) = std::env::var("AUTO_SUBTITLE_LANG") {
            config.auto_subtitle_lang = lang;
        }

        Ok(config)
    }

    fn from_env_content(content: &str) -> Result<Self> {
        let mut env_vars: HashMap<String, String> = HashMap::new();

        for line in content.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some((key, value)) = line.split_once('=') {
                let value = value.trim().trim_matches('"').trim_matches('\'').to_string();
                env_vars.insert(key.trim().to_string(), value);
            }
        }

        let mut config = Self::default();

        if let Some(host) = env_vars.get("HOST") {
            config.host = host.clone();
        } else if let Some(host) = env_vars.get("APP_HOST") {
            config.host = host.clone();
        }

        if let Some(port) = env_vars.get("PORT") {
            if let Ok(p) = port.parse() {
                config.port = p;
            }
        } else if let Some(port) = env_vars.get("APP_PORT") {
            if let Ok(p) = port.parse() {
                config.port = p;
            }
        }

        if let Some(ws) = env_vars.get("WORKSPACE_DIR") {
            config.workspace_dir = ws.clone();
        }

        if let Some(backend) = env_vars.get("DEFAULT_BACKEND") {
            config.default_backend = backend.clone();
        }

        if let Some(model) = env_vars.get("DEFAULT_FUNASR_MODEL") {
            config.default_funasr_model = model.clone();
        }

        if let Some(model) = env_vars.get("DEFAULT_WHISPER_MODEL") {
            config.default_whisper_model = model.clone();
        }

        if let Some(lang) = env_vars.get("AUTO_SUBTITLE_LANG") {
            config.auto_subtitle_lang = lang.clone();
        }

        // 加载模型配置
        config.model_profiles = Self::load_model_profiles(&env_vars)?;
        if let Some(active) = env_vars.get("ONLINE_MODEL_ACTIVE_PROFILE") {
            config.active_profile = active.clone();
        }

        Ok(config)
    }

    fn load_model_profiles(env_vars: &HashMap<String, String>) -> Result<Vec<ModelProfile>> {
        let mut profiles = Vec::new();

        let count: usize = env_vars
            .get("ONLINE_MODEL_PROFILE_COUNT")
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);

        for i in 1..=count {
            let prefix = format!("ONLINE_MODEL_PROFILE_{i}_");
            let name = env_vars
                .get(&format!("{prefix}NAME"))
                .cloned()
                .unwrap_or_default();

            if name.is_empty() {
                continue;
            }

            profiles.push(ModelProfile {
                name,
                base_url: env_vars
                    .get(&format!("{prefix}BASE_URL"))
                    .cloned()
                    .unwrap_or_default(),
                api_key: env_vars
                    .get(&format!("{prefix}API_KEY"))
                    .cloned()
                    .unwrap_or_default(),
                default_model: env_vars
                    .get(&format!("{prefix}DEFAULT_MODEL"))
                    .cloned()
                    .unwrap_or_default(),
            });
        }

        if profiles.is_empty() {
            profiles.push(ModelProfile {
                name: "default".to_string(),
                base_url: "https://api.siliconflow.cn/v1".to_string(),
                api_key: env_vars.get("SILICONFLOW_API_KEY").cloned().unwrap_or_default(),
                default_model: "Pro/moonshotai/Kimi-K2.5".to_string(),
            });
        }

        Ok(profiles)
    }

    pub fn get_active_profile(&self) -> Option<&ModelProfile> {
        self.model_profiles
            .iter()
            .find(|p| p.name == self.active_profile)
            .or_else(|| self.model_profiles.first())
    }

    pub fn workspace_path(&self) -> PathBuf {
        PathBuf::from(&self.workspace_dir)
    }
}
