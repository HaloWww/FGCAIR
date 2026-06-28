# FGCAir Home Assistant 自定义集成

## 安装

把 `custom_components/fgcair` 目录复制到 Home Assistant 配置目录的 `custom_components/fgcair`，然后重启 Home Assistant。

## 配置

在 Home Assistant 中进入：

`设置 -> 设备与服务 -> 添加集成 -> FGCAir`

配置流程：

1. 输入 FGCAir 账号和密码。
2. 可勾选自动绑定抓包中的网关。
3. 在下拉框中选择要创建空调实体的室内机。

## HomeKit

本集成创建的是标准 `climate` 实体。安装后在 Home Assistant 的 HomeKit Bridge 集成里选择这些 `climate.fgcair_*` 实体即可桥接到 HomeKit。

## Token

集成会保存 token，调用接口时发现 token 过期会自动用账号密码重新登录。也可以在开发者工具中调用服务：

`fgcair.refresh_token`

手动刷新 token。

## 状态说明

FGCAir 私有云的 HTTP 状态缓存经常为空。本集成优先读取云端状态；如果云端没有状态，会使用最近一次由本集成成功写入的开关、模式、温度和风速作为实体状态。
