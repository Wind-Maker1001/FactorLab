# P4-D02：601857.SH 官方历史资料与供应商差异定向补证

2026-11-09；窗口2017-01-03—2018-12-28；起点及远端核实基线`4b1baad1f661be5728cfb79ee9a4db5bc1b9f32f`。

**PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

本轮新增是当前BigQuant公开字段说明的响应字节、官方归档访问结果和四金额的可复现实验，不是新的历史公告原件或独立历史名单。未取得两份2017官方PDF、官方样本起点或完整变更链。P4-D01的38项状态不变：13 PASS、2 FAIL、19 UNKNOWN、4 NOT_APPLICABLE；原始矩阵、索引和收据未重写，绑定见本轮receipt.json。

## R03：表示距离闭合，供应商处理链仍未闭合

只读取D05已保存的四条受控私有诊断，未重读大型原生工作簿、两年全量行情或供应商ZIP。该附件含CS原生十进制字符串与两源binary64表示及版本；本轮实验重新计算，非重新取得供应商原始输入。公开输出AMOUNT_EXPERIMENT.json不含商业绝对金额，仅差值、布尔判断及输入哈希。

|日期|BQ−CS精确差值|signed ULP|CS原生工作簿物理行|
|---|---:|---:|---:|
|2017-02-27|−0.0000000298023223876953125|−1|987376|
|2017-05-03|−0.0000000298023223876953125|−1|987423|
|2018-10-12|+0.000000059604644775390625|+1|987800|
|2018-12-13|+0.0000000298023223876953125|+1|987844|

四个CS原生十进制输入均可被binary64精确表示，转换后与CS prepared相等；BQ保存值均为对应CS值沿差值方向的相邻binary64。因此这四例不能归因为“CS原生十进制本身无法精确转换为double”。BQ的原始十进制输入、上游计算和单位转换路径仍未知。0/2/4/6/8位十进制量化只作候选实验，未修改比较输入，未以候选吻合认证算法；精确跨源条件继续FAIL，RAW_amount_cause继续UNKNOWN / UNRESOLVED。

[BigQuant当前原价页](https://bigquant.com/data/datasources/cn_stock_real_bar1d)本轮可读，声称amount为“成交额，单位为元，是当日累计值”、volume单位为股。这是新增的**VENDOR_CLAIM_UNVERIFIED**：当前页面字节并未证明2017—2018适用版本、币种、盘后/大宗等成交类型、舍入位置、内部十进制精度或修订。不能将本轮页面倒填为历史单位认证。CSMAR沿用已审DES及profiles映射CNY、乘数1；不是新增供应商回复。

## R04：未取得可认证的历史成员链

检索限定000905.SH中证500。CSI主页和静态前端可读；只把脚本文本当数据定位官方公告接口，没有执行下载脚本。搜索接口及历史公告查询queryAnnouncementByVo返回HTTP 200但JSON code=500、data=null；一个猜测分页路径404，明确不是已证明的正式归档端点。queryAnnouncementByType可取得当前公告分组，返回2026内容，不能作为2016—2018历史检索完整性或零结果证明。保存完整请求URL及响应摘要；未证明查询参数是官方接受的历史过滤合同，因此不声称已穷尽官方档案。

历史起点、2016年末生效名单、2017/2018四次定期及所有适用临时/条件调整、完整性证明、版本/修订和证券身份映射均缺。MEMBERSHIP_EVIDENCE.csv只记录两个窗口边界的UNKNOWN，覆盖说明明确未覆盖中间会话；不虚构487个已核验结论。BQ487日、目标成员/变更各0及JQ完整包2021起是**PREVIOUS_EVIDENCE_REUSED**，不是本轮重扫，也不证明非成员。PIT_membership保持UNKNOWN，五年成员门禁不变。

## R09：仍缺2017正式PDF

本轮直接访问既有两个static.sse正式定位URL，均HTTP 200、Content-Type text/html、3872字节gzip载荷；解压为HTML/JavaScript挑战响应，不是PDF。两响应本轮同摘要`d88101bcd9231bc1fa3dea509b7f878b7a42816abc198ef700d0af7ad142b837`，与D05旧响应摘要不同。只说明响应字节发生变化，不能解释成公告修订。发行人年度目录及月目录返回412。本轮没有新增官方公告PDF，也未重新下载2018原件或既有镜像。

2017-025/041的标题、编号、金额、登记/除息/支付和税务仍继承D04镜像调查及D05索引，标记PREVIOUS_EVIDENCE_REUSED；当前官方端点原件MISSING_ORIGINAL，不能把挑战响应标为THIS_RUN_ORIGINAL_VERIFIED。六月0.03801（普通0.01801+特别0.02）、九月0.06926（普通0.03117+特别0.03809）元/旧股条款未升级证据等级。页数及正文未在本轮由官方原件验证；是否更正/替换仍UNKNOWN。2017-041落款9月7日与SSEDATE 9月8日分别保留；effective_at为6月22日和9月15日；known_at继续UNKNOWN。

## 顺带取得的R07材料与R01/R02边界

[当前后复权页](https://bigquant.com/data/datasources/cn_stock_bar1d)声称调整价=未复权价×累计adjust_factor，量额与原价表一致。[当前事件因子页](https://bigquant.com/data/datasources/cn_stock_adjust_factor)声称每事件forward/backward两行、互为倒数，price_gap使用前一交易日收盘和当日前收盘比值，累计后向为backward连乘。这缩小R07待询证语义，但未给历史算法版本、绝对初始锚点、参考价内部精度和修订重算合同，更不是独立HFQ参考。两页均为VENDOR_CLAIM_UNVERIFIED，HFQ相关条件保持UNKNOWN。

本轮没有取得R01/R02的真实历史版本日志或首次分发记录。最小请求已写为可直接提交的[VENDOR_REQUESTS.md](VENDOR_REQUESTS.md)，没有发送邮件、提交支持工单或声称供应商已回复。

## 状态及下一轮最小优先级

局部新增事实仅为当前公开页面语义和四条表示实验；EVIDENCE_UPDATES.csv记录条件关联，不生成新PASS。仍FAIL的两项为RAW_exact_cross_source_values、FORMAL_admission。19个UNKNOWN原样保留，包括成员/身份/状态/日历、历史分发修订、金额原因及换手合同、HFQ算法/锚点/独立参考/事件修订、两份2017原件及四类非现金事件完整性。四个现金新增股份上市日仍仅限各自事件NOT_APPLICABLE。

1. R03：向两源支持提交四记录处理链请求，尤其BQ原始十进制、单位转换和舍入阶段；不再重取全量行情。
2. R09：通过发行人/交易所档案渠道索取两份正式PDF及发布/替换日志；避免继续重复同一挑战端点。
3. R04：索取覆盖2017-01-03的官方完整起点及截至2018-12-28完整适用变更链，或有版本/覆盖证明的逐日名单；当前API失败不是无历史资料证明。
4. R01/R02/R07：以现有内容身份请求分发/修订日志和历史算法合同；独立价格参考仍须R08。

TODO：取得上述原件或合同后仅更新证据支持的日期/条件；不按UNKNOWN减少数量验收，不补历史首次可得时间。

## 复现与字节身份

SOURCE_INDEX.json记录每次新响应的原始URL、实际UTC采集时间、SHA-256、类型与失败语义；sources/每响应独立目录保留字节与收据。当前公开页面取得字节不称历史原件核验。HTTP错误也保留状态；首个发行人请求仅取得异常记录，无响应字节摘要，明确UNKNOWN。receipt.json绑定本轮文件及P4-D01四个基线文件，文件自身不作自引用摘要。

在可信工作树根运行，PY313为既有Python 3.13环境，PRIVATE为D05保存的amount-details-final.json（授权私有路径，不提交）：

```sh
<PY313> -I -B -X utf8 tools/evidence/audit_p4_d02.py --private-sample <PRIVATE> --output <NEW_PUBLIC_SUMMARY.json>
<PY313> -I -B -X utf8 -c "import sys; sys.path.insert(0, 'src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/evidence/test_p4_d02_evidence.py', 'tests/evidence/test_p4_d01_evidence.py', 'tests/evidence/test_p3_d05.py']))"
```

新测试验证私有四记录严格范围/版本、表示差值、量化不吞冲突、不认证单位/known_at、拒绝表达式输入和公开输出无商业绝对值；复用D01/D05相关负例验证公告索引/字节、挑战页、哈希漂移、公告/实施角色、缺名单链不认证、HFQ缺独立参考及正式门禁。定向组合实际 **46 passed / 1.54秒**，见tests.xml。
