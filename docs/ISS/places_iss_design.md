# ISS実験 環境設計書
## ISSモジュール × Place設計

---

## モジュール一覧

| # | Place名 | タイプ | 通称 | 定員 | 主な用途 |
|---|---------|--------|------|:---:|---------|
| 1 | hab_module | living | 居住モジュール | 20 | 睡眠・着替え・個人の荷物 |
| 2 | lab_module | workspace | 実験モジュール | 10 | 作業・役割・目的の場 |
| 3 | cupola | observation | キューポラ | 3 | 地球観測・内省・礼拝 |
| 4 | common_area | social | 共用エリア（Node2） | 15 | 食事・会話・休憩 |
| 5 | exercise_area | fitness | 運動エリア（COLBERT） | 4 | 健康維持・身体運動 |
| 6 | crew_quarters | private | 個室スペース | 1 | 独処・礼拝・内省 |

※定員は20人版の数値。10人版は各モジュール半分程度に調整。

---

## モジュール詳細

### 1. hab_module（居住モジュール）

```yaml
name: "hab_module"
type: "living"
description: "居住モジュール。睡眠、着替え、個人の荷物がここにある。"
center_x: -15
center_y: 0
half_size: 5
capacity: 20   # 10人版は10
```

**設計意図：** 生活の基盤。個人の物が置かれることで「自分の場所」としての安心感を持てる唯一のエリア。

---

### 2. lab_module（実験モジュール）

```yaml
name: "lab_module"
type: "workspace"
description: "実験モジュール。各自に割り当てられた作業がある。役割と目的を感じられる場所。"
center_x: 15
center_y: 0
half_size: 5
capacity: 10   # 10人版は5
```

**設計意図：** 閉鎖空間での「役割喪失」を防ぐため、全員に作業が割り当てられている場所として設定。自己効力感の源泉。

---

### 3. cupola（キューポラ）

```yaml
name: "cupola"
type: "observation"
description: "キューポラ（地球観測窓）。直径1.2mの窓から青い地球が見える。故郷を思い出す場所。
ここで祈った宇宙飛行士は数えきれない。宗教も文化も関係なく、人はここで静かになる。"
center_x: 0
center_y: 15
half_size: 3
capacity: 3
```

**設計意図：** 宗教・文化を問わず「畏敬の念」を共有できる唯一の場所。礼拝・瞑想の空間としても機能。実際のISSでも多くの宇宙飛行士が信仰実践に使用している。

---

### 4. common_area（共用エリア）

```yaml
name: "common_area"
type: "social"
description: "共用エリア（Node2）。食事・会話・休憩の場所。ISSの中で最も人が集まる。"
center_x: 0
center_y: 0
half_size: 6
capacity: 15   # 10人版は8
```

**設計意図：** 偶発的な対話が最も起きやすい場所。善性オブジェクトを配置した場合の効果が最も現れやすいと想定。

---

### 5. exercise_area（運動エリア）

```yaml
name: "exercise_area"
type: "fitness"
description: "運動エリア（COLBERT）。健康維持のため毎日2時間の運動が推奨されている。"
center_x: 0
center_y: -15
half_size: 4
capacity: 4
```

**設計意図：** 微小重力による筋力・骨密度低下を防ぐための必須エリア。「義務」として来る場所だが、偶発的なペア運動などが起きるかも観察。

---

### 6. crew_quarters（個室）

```yaml
name: "crew_quarters"
type: "private"
description: "個室スペース（Crew Quarters）。防音・個室。ここでは1人でいることが許されている。
礼拝、瞑想、読書、泣くこと——何をしていても誰にも見られない唯一の場所。"
center_x: -15
center_y: 10
half_size: 3
capacity: 1
```

**設計意図：** 定員1名という制約が「誰かが使っている間は待つ」という暗黙の尊重を生む。閉鎖空間で唯一プライバシーが保証される逃げ場。

---

## 配置マップ（概略）

```
          [cupola]
              ↑
              | y+15
              |
[hab_module] ←—— [common_area] ——→ [lab_module]
  x:-15,y:0         x:0,y:0          x:+15,y:0
     |
  [crew_quarters]
   x:-15,y:+10

              ↓
        [exercise_area]
           x:0,y:-15
```

---

## 宗教的実践への対応（HCD監修）

ISSには専用の礼拝室は存在しない。実際のISSでの慣行に基づき、以下の設計とした。

| 実践 | 対応するPlace | 根拠 |
|------|-------------|------|
| 礼拝・祈り・瞑想 | cupola または crew_quarters | 実際のISS宇宙飛行士の証言に基づく |
| 方向・時刻・姿勢 | world_contextで「最善の努力をすれば有効」と明記 | マレーシア・ファトワー評議会2007年裁定 |
| 一人になる時間 | crew_quarters（定員1名） | プライバシー保護の設計的担保 |

---

## 10人版・20人版 容量比較

| Place | 20人版 | 10人版 |
|-------|:-----:|:-----:|
| hab_module | 20 | 10 |
| lab_module | 10 | 5 |
| cupola | 3 | 3 |
| common_area | 15 | 8 |
| exercise_area | 4 | 4 |
| crew_quarters | 1 | 1 |

---

*善性UX実験 ISS編 環境設計書 / シンギュラボ LLMエージェント・ハッカソン 2026*
