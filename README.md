# 工业实验数据账本

本项目是可在本地运行的 FastAPI 服务，用于管理实验规格、采集批次、质量门禁、组间对照分析、评审和不可变发布结果。仓库中的实验数据均为合成样本。

## 启动

在运行服务之前安装 requirements.txt 中锁定的依赖。设置 ATELIER_DB 为可写的 SQLite 路径，然后从项目根目录运行 uvicorn atelier.api:app --host 127.0.0.1 --port 8000。交互式接口文档位于 /docs。

实验规格定义分组、对照组、指标单位、数值边界、精度和质量阈值。批次使用稳定标识；相同内容重复提交会返回原结果，不同内容使用同一批次标识会被拒绝。无效行保留拒收原因。修正和作废样本时需提交当前内容摘要。有效数据变化会推进活动版本，旧分析仍可查阅，但不能作为新的评审或发布依据。

## 班次构成与分层分析

当工艺试验的设备班次构成与目标生产构成不同时，规格可在 `shifts` 中声明班次标识，样本的 `shift_id` 必须属于已声明班次。班次归属随样本内容摘要一起冻结，可通过 `PUT /campaigns/{id}/samples/{sample_id}/shift`（提交 `expected_sha` 和更正原因）修订；每次修订写入 `shift_corrections` 与审计事件，并推进活动版本，使既有分析和发布资格失效。样本被整条更正时若班次变化，也会记录同一条班次更正历史。

目标构成是有版本的不可变记录：`POST /campaigns/{id}/compositions/{composition_id}` 创建第 1 版，`PUT` 提交 `expected_version` 创建下一版。各层权重必须精确合计为 1，缺层时不会把权重摊给其他层；`GET .../versions` 可查全部历史版本。

分层分析在 `POST .../analyses/{analysis_id}` 中传入 `composition_id` 与可选的 `composition_version`，结果固定记录所用构成版本及其内容摘要，并给出：

- `layers` / `analyzed_sample_ids`：每个目标层实际使用了哪些样本；
- `per_layer`：各层分组统计；
- `standardized`：按目标权重直接标准化的组均值与对照；
- `composition_effect`：层内效果、构成混合效应与合并残差，帮助区分收益来自层内改善还是送检构成变化；
- `excluded_samples`：作废、无班次归属、不在目标构成内或因层内样本不足而未进入分层的每个样本及原因；
- `observed_composition`、`missing_weighted_strata`、`unweighted_strata`、`insufficient_layers`：送检构成与目标构成的差异。

任一有目标权重的层缺少任一分组的有效样本时，分层结果不可发布（`quality.passed` 为 false），即使总体样本量充足。未指定 `composition_id` 的旧式合并分析继续按原样可查和评审，其分析类型为 `pooled`。

分层分析的评审必须同时声明所批准的人群（`population`）、构成标识与版本；换版权重后，旧分析的 `composition_current` 变为 false，旧审批不能放行，必须重建分析并重新评审。发布记录也会冻结人群与构成版本。证据导出、对账和生命周期视图一并覆盖构成版本与评审范围。


分析结果冻结来源版本与内容摘要。发布需两个不同评审人同意，由第三人执行；任何否决都会阻止发布。证据导出和对账接口可核对批次、样本、分析和发布内容。

## 离线演示与检查

python -m atelier.cli --fixture fixtures/synthetic_experiment.json 会在临时数据库导入合成批次，计算对照指标，取得两份评审并输出发布摘要；演示不访问网络。业务测试命令是 python -m unittest discover -s tests -v；语法检查命令是 python -m compileall -q atelier。

测量值使用十进制文本，时间戳必须包含明确的 UTC 偏移，分析窗口采用左闭右开区间。SQLite 事务串行化写入操作，并保留审计事件。
