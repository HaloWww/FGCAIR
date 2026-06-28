# FGCAir Home Assistant 自定义集成

这是 FGCAir/机智云私有云的 Home Assistant 自定义集成，提供标准 `climate` 空调实体，可通过 Home Assistant HomeKit Bridge 桥接到 HomeKit。

## 安装

把仓库中的 `custom_components/fgcair` 目录复制到 Home Assistant 配置目录：

```text
config/custom_components/fgcair
```

然后重启 Home Assistant。

## 配置

进入 `设置 -> 设备与服务 -> 添加集成 -> FGCAir`。

配置流程：

1. 输入 FGCAir 账号和密码。
2. 可选择自动绑定抓包中的网关。
3. 在下拉框中选择要接入 Home Assistant 的室内机。

## HomeKit

本集成创建标准 `climate` 实体。安装并配置后，在 Home Assistant 的 HomeKit Bridge 集成中选择 `climate` 实体即可桥接到 HomeKit。

项目使用 `bigmoby/fglair_for_homeassistant` 的 `FGLair_logo.png` 作为图标，并按 Home Assistant 官方自定义集成品牌图规范同步到 `custom_components/fgcair/brand/icon.png` 与 `logo.png`。

## Token

集成会保存 token，并在发现 token 过期时自动使用用户名密码重新登录。也可以在开发者工具中调用服务手动刷新：

```text
fgcair.refresh_token
```

## 状态

FGCAir 私有云的 HTTP 状态缓存经常为空。本集成会优先读取云端状态；如果云端没有状态，会使用最近一次由本集成成功写入的开关、模式、温度和风速作为实体状态。

首次加载时默认状态为：关闭、制冷、26℃、风速自动。这样即使云端状态缓存为空，Home Assistant 和 HomeKit 也能显示并下发基础控制。

## 控制排查

如果在 `http://192.168.8.11:8123` 中无法控制设备：

1. 重启 Home Assistant，确保 custom_components 更新已加载。
2. 在开发者工具中调用 `fgcair.refresh_token` 刷新 token。
3. 在实体详情中确认已选择正确的 `climate` 实体。
4. 查看 Home Assistant 日志中是否有 `fgcair` 或 `/app/control/{did}` 的错误。
