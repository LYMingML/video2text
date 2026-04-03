use anyhow::Result;
use tracing::{info, error};
use std::net::SocketAddr;

mod api;
mod core;
mod ml;
mod utils;

use core::config::Config;
use core::job::JobManager;

#[tokio::main]
async fn main() -> Result<()> {
    // 初始化日志
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    info!("Starting video2text-rs server...");

    // 加载配置
    let config = Config::load()?;
    info!("Configuration loaded: port={}, host={}", config.port, config.host);

    // 创建工作目录
    let workspace = std::path::PathBuf::from(&config.workspace_dir);
    tokio::fs::create_dir_all(&workspace).await?;
    tokio::fs::create_dir_all(workspace.join("temp_video")).await?;

    // 初始化任务管理器
    let job_manager = JobManager::new();

    // 构建路由
    let app = api::create_router(job_manager, config);

    // 启动服务器
    let addr: SocketAddr = format!("{}:{}", config.host, config.port).parse()?;
    info!("Server listening on http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}
