# APO2PH4 — PocketHunter Entegrasyonu

## İçindekiler
1. [Genel Bakış](#genel-bakış)
2. [Ortam Kurulumu](#adım-0--ortam-kurulumu)
3. [Farmakofor Modeli Üretimi](#adım-1--farmakofor-modeli-üretimi)
4. [Ligand Tarama (Screening)](#adım-2--ligand-tarama)
5. [Hata Çözümleri](#olası-hatalar-ve-çözümleri)

---

## Genel Bakış

Apo2ph4 workflow'u 4 aşamadan oluşur:

```
PDB + pocket_center → [1] define_binding_site → [2] fragment docking (Vina)
    → [3] interaction pharmacophore (CDPKit) → [4] final pharmacophore model (.pml)
```

Her cluster representative için bu workflow tekrarlanır. Sonra her farmakofor modeliyle actives/decoys taranır.

**Önemli:** Apo2ph4 sadece model üretir. Screening (ligand tarama) ayrı bir adımdır ve bu prompt'un ADIM 2'sinde CDPKit ile yapılır.

---

## ADIM 0 — Ortam Kurulumu (bir kereye mahsus)

```bash
# Conda environment oluştur
conda config --add channels conda-forge
conda config --add channels bioconda
conda create -n apo2ph4 python==3.10 -y
conda activate apo2ph4

# Bağımlılıkları kur
conda install -y pymol-open-source openbabel autodock autogrid autodock-vina mgltools
pip install cdpkit scikit-learn

# Apo2ph4 reposunu klonla
cd ~/workspace
git clone https://github.com/molinfo-vienna/apo2ph4.git
cd apo2ph4

# Dizin yapısı
mkdir -p ~/workspace/p38a_ph4_results/{cluster_0,cluster_1}
```

**Doğrulama:**
```bash
conda activate apo2ph4
python -c "import CDPL; print('CDPKit OK')"
python -c "import pymol; print('PyMOL OK')"
which vina && echo "Vina OK"
which autogrid4 && echo "AutoGrid OK"
```

---

## ADIM 1 — Farmakofor Modeli Üretimi

Her cluster representative için aşağıdaki script'i çalıştır. Script tüm 4 aşamayı otomatik yapar.

Önce pocket center koordinatlarını bul:

```bash
conda activate apo2ph4

# p2rank pockets CSV'den ilk pocket'ın center'ını al
# (Her frame için aynı pocket center kullanılabilir, pocket hareket etmiyorsa)
python3 -c "
import pandas as pd
df = pd.read_csv(os.path.expanduser('~/workspace/PocketHunter-Suite/md_simulation/p38a/pockethunter_test/pockets/p2rank_output/1kv1_200_pockets.csv'))
row = df.iloc[0]
print(f'{row.center_x:.3f} {row.center_y:.3f} {row.center_z:.3f}')
"
```

Çıkan koordinatları not et (örnek: `12.345 -5.678 30.123`).

### Cluster 0 (Frame 200) için:

```bash
conda activate apo2ph4
cd ~/workspace/apo2ph4

# === Aşama 1: Binding site tanımla ===
REP_PDB=~/workspace/PocketHunter-Suite/md_simulation/p38a/pockethunter_test/pdbs/1kv1_200.pdb
CX=12.345   # ← yukarıdaki pocket center X
CY=-5.678   # ← yukarıdaki pocket center Y  
CZ=30.123   # ← yukarıdaki pocket center Z
OUTDIR=~/workspace/p38a_ph4_results/cluster_0

mkdir -p $OUTDIR
cp $REP_PDB $OUTDIR/original.pdb

python3 apo2ph4_define_binding_site.py $REP_PDB $CX $CY $CZ
# → 1kv1_200_prepared.pdb oluşur

# === Aşama 2: Fragment docking ===
bash apo2ph4_prepare_and_dock.sh data/fragments.sdf 1kv1_200_prepared.pdb
# → *.map grid dosyaları, complex_list.list, feature_count.txt oluşur
# Süre: ~30-60 dk

# === Aşama 3: Interaction pharmacophore çıkar ===
bash apo2ph4_generate_docked_frag_ph4s_cdpkit.sh
# → pharmacophores/pharmacophores.pml oluşur
# Süre: ~1-2 dk

# === Aşama 4: Final farmakofor modeli ===
python3 apo2ph4_generate_ph4.py \
  -i pharmacophores/pharmacophores.pml \
  -o $OUTDIR/pharmacophore.pml \
  -g . \
  -p "p38a_Cluster0" \
  --max_hydrophobic 5 \
  --max_HBD 3 \
  --max_HBA 3 \
  --max_PI 2 \
  --max_NI 2
# → $OUTDIR/pharmacophore.pml oluşur
# Süre: ~1 dk

# === Temizlik (sonraki cluster için) ===
# Grid map'leri ve dock çıktılarını temizle
rm -f *.map feature_count.txt complex_list.list *_prepared.pdb
rm -rf tempdock pharmacophores
```

### Cluster 1 (Frame 10000) için:

```bash
conda activate apo2ph4
cd ~/workspace/apo2ph4

REP_PDB=~/workspace/PocketHunter-Suite/md_simulation/p38a/pockethunter_test/pdbs/1kv1_10000.pdb
# Aynı pocket center'ı kullan (pocket aynı proteinin farklı frame'i)
CX=12.345
CY=-5.678
CZ=30.123
OUTDIR=~/workspace/p38a_ph4_results/cluster_1

mkdir -p $OUTDIR
cp $REP_PDB $OUTDIR/original.pdb

python3 apo2ph4_define_binding_site.py $REP_PDB $CX $CY $CZ
bash apo2ph4_prepare_and_dock.sh data/fragments.sdf 1kv1_10000_prepared.pdb
bash apo2ph4_generate_docked_frag_ph4s_cdpkit.sh
python3 apo2ph4_generate_ph4.py \
  -i pharmacophores/pharmacophores.pml \
  -o $OUTDIR/pharmacophore.pml \
  -g . \
  -p "p38a_Cluster1" \
  --max_hydrophobic 5 \
  --max_HBD 3 \
  --max_HBA 3 \
  --max_PI 2 \
  --max_NI 2

rm -f *.map feature_count.txt complex_list.list *_prepared.pdb
rm -rf tempdock pharmacophores
```

### Doğrulama

```bash
ls -la ~/workspace/p38a_ph4_results/cluster_0/pharmacophore.pml
ls -la ~/workspace/p38a_ph4_results/cluster_1/pharmacophore.pml
# Her iki dosya da ~5-50 KB arası olmalı

# İçeriğe hızlıca bak
head -30 ~/workspace/p38a_ph4_results/cluster_0/pharmacophore.pml
# <FeatureContainer> ve <Feature> tag'leri görmelisin
```

---

## ADIM 2 — Ligand Tarama

Farmakofor modelleri hazır olduktan sonra screening script'ini çalıştır.

**Bu script PocketHunter-Suite reposunda `scripts/screen_ph4.py` olarak gelecek.**
Çalıştırmak için:

```bash
conda activate apo2ph4
cd ~/workspace/PocketHunter-Suite

python scripts/screen_ph4.py \
  --pharmacophore ~/workspace/p38a_ph4_results/cluster_0/pharmacophore.pml \
  --actives ~/workspace/PocketHunter-Suite/md_simulation/p38a/dude_mk14/actives_final.sdf \
  --decoys ~/workspace/PocketHunter-Suite/md_simulation/p38a/dude_mk14/decoys_final.sdf \
  --out ~/workspace/p38a_ph4_results/cluster_0/screening_results.csv

python scripts/screen_ph4.py \
  --pharmacophore ~/workspace/p38a_ph4_results/cluster_1/pharmacophore.pml \
  --actives ~/workspace/PocketHunter-Suite/md_simulation/p38a/dude_mk14/actives_final.sdf \
  --decoys ~/workspace/PocketHunter-Suite/md_simulation/p38a/dude_mk14/decoys_final.sdf \
  --out ~/workspace/p38a_ph4_results/cluster_1/screening_results.csv
```

ROC-AUC sonuçlarını görmek için:

```bash
python3 -c "
import pandas as pd
from sklearn.metrics import roc_auc_score

for cluster in ['cluster_0', 'cluster_1']:
    df = pd.read_csv(f'~/workspace/p38a_ph4_results/{cluster}/screening_results.csv')
    df = df.expanduser()
    auc = roc_auc_score(df['label'], df['score'])
    print(f'{cluster}: ROC-AUC = {auc:.4f}')
"
```

---

## Beklenen Süre

| Adım | Süre |
|------|------|
| Ortam kurulumu | ~20 dk |
| Cluster başına fragment docking (100 fragment) | ~30-60 dk |
| Cluster başına farmakofor üretimi | ~3 dk |
| Cluster başına ligand tarama (1000 ligand) | ~2-5 dk |
| **Toplam (2 cluster, paralel değil)** | **~2-3 saat** |

---

## Olası Hatalar ve Çözümleri

### 1. `prepare_receptor4.py` bulunamadı
```bash
export PATH=$PATH:$CONDA_PREFIX/MGLToolsPckgs/AutoDockTools/Utilities24
```

### 2. OpenBabel `babellib` hatası
```bash
export BABEL_LIBDIR=$CONDA_PREFIX/lib/openbabel/$(obabel -V | cut -f 3 -d ' ')
export BABEL_DATADIR=$CONDA_PREFIX/share/openbabel/$(obabel -V | cut -f 3 -d ' ')
```
Bu satırlar `prepare_and_dock.sh` içinde otomatik çalışır. Elle çalıştırmana gerek olmamalı.

### 3. `pymol` import hatası
```bash
conda activate apo2ph4
python -c "import pymol"  # Hata veriyorsa:
conda install -y -c conda-forge pymol-open-source
```

### 4. Grid box pocket'ı kapsamıyor
`write_vina_conf.py` dummy benzene'in ağırlık merkezine göre 20×20×20 Å grid box oluşturur. Eğer pocket bu boyuttan büyükse, `vinaconf.txt` dosyasını manuel düzenle:
```
size_x = 25
size_y = 25
size_z = 25
```

### 5. CDPKit `import` hatası
```bash
pip install --force-reinstall cdpkit
python -c "import CDPL.Pharm; print('OK')"
```

### 6. `feature_count.txt not found`
Bu dosya `count_features.py` tarafından oluşturulur. `prepare_and_dock.sh` otomatik çalıştırır. Eksikse manuel çalıştır:
```bash
python3 scripts/count_features.py data/fragments.sdf
```
