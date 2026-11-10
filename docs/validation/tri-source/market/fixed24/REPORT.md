# 三源行情与交易状态审计

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

逐源、两两及三源分母独立保存在 coverage.csv；comparisons.csv 区分同义精确比较与未认证单位的数值观察。原生十进制直接比较，无任意容差。原价不与复权价混合。差异键和原件坐标保存在 anomalies.csv，完整原值只留本地。

运行状态及真实范围见 summary.json。BigQuant 单位和历史枚举合同缺失，不据数值相等授予一致性认证；三源可能共享上游，三源相同不等于独立正确。
