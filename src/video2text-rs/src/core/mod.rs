pub mod config;
pub mod job;
pub mod workspace;

use serde::{Deserialize, Serialize};

/// 字幕片段
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Segment {
    pub start: f64,
    pub end: f64,
    pub text: String,
}

impl Segment {
    pub fn new(start: f64, end: f64, text: impl Into<String>) -> Self {
        Self {
            start,
            end,
            text: text.into(),
        }
    }
}

/// 转录结果
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptionResult {
    pub segments: Vec<Segment>,
    pub language: Option<String>,
    pub duration: f64,
}

/// 任务元数据
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskMeta {
    pub file_prefix: String,
    pub lang_code: String,
    pub source_lang: String,
    pub is_non_zh: bool,
}
