<!-- Text direction: Right-to-Left (Persian / فارسی) -->

# جدول فهرست افزونه‌ها (Plugin Index)

> این جدول مرجع سریع همه‌ی افزونه‌های Carto است: کار هر افزونه، فایل‌های فعال هر سکو،
> و وضعیت نسخه‌ی قدیمی.

[بازگشت به راهنمای کلی](00_Carto_Overview_FA.md)

---

## نمای کلی

| شماره | نام | کار اصلی (یک‌خطی) | راهنمای فارسی |
|:----:|------|-------------------|----------------|
| ۰۱ | پل و آبگذر | نماد پل/آبگذر در تقاطع جاده×آبراهه + تنظیم زاویه | [Plugin01](../plugins/Plugin01_BridgeCulvert_FA.md) |
| ۰۲ | جداسازی از جاده | دورکردن عوارض از جاده برای فاصله‌ی ایمن | [Plugin02](../plugins/Plugin02_RoadDeconflict_FA.md) |
| ۰۳ | بهینه‌ساز برچسب منحنی | بهترین نقطه برای برچسب روی منحنی میزان | [Plugin03](../plugins/Plugin03_ContourLabelOptimizer_FA.md) |
| ۰۴ | جداسازی متن ارتفاع | جابه‌جایی متن ارتفاع تا روی موانع نیفتد | [Plugin04](../plugins/Plugin04_ElevationTextDeconflict_FA.md) |
| ۰۵ | پاک‌سازی امن منحنی | حذف ایمن منحنی‌های متراکم در ناحیه‌ی مشخص | [Plugin05](../plugins/Plugin05_SafeContourCleaner_FA.md) |
| ۰۶ | چرخش نماد چشمه | محاسبه‌ی زاویه‌ی چرخش چشمه بر اساس شیب | [Plugin06](../plugins/Plugin06_SpringRotation_FA.md) |
| ۰۷ | سازنده‌ی دسته‌ای شبکه | ساخت دسته‌ای شبکه/گراتیکول روی چند برگه | [Plugin07](../plugins/Plugin07_BatchGridBuilder_FA.md) |

---

## جزئیات فایل‌ها و نسخه‌ها

| شماره | فایل ArcMap (پایتون ۲٫۷) | فایل Pro (پایتون ۳) | تعداد ابزار | نسخه‌ی قدیمی ثبت‌شده |
|:----:|---------------------------|----------------------|:----:|----------------------|
| ۰۱ | `Plugin01_BridgeCulvert_ArcMap_py27.pyt` | `Plugin01_BridgeCulvert_Pro_py3.pyt` | ۴ | — (ندارد) |
| ۰۲ | `Plugin02_RoadDeconflict_ArcMap_v5_hardened.pyt` | `Plugin02_RoadDeconflict_Pro_v5_native.pyt` | ۱ | `..._v4_fixedUIUX_py27.pyt` (موجود نیست) |
| ۰۳ | `Plugin03_ContourLabelOptimizer_ArcMap_v4_hardened.pyt` | `Plugin03_ContourLabelOptimizer_Pro_v4_native.pyt` | ۲ | `..._v3.pyt` (موجود نیست) |
| ۰۴ | `Plugin04_ElevationTextDeconflict_ArcMap_v5_hardened.pyt` | `Plugin04_ElevationTextDeconflict_Pro_v5_native.pyt` | ۱ | `Plugin04.pyt` (موجود نیست) |
| ۰۵ | `Plugin05_SafeContourCleaner_ArcMap_v5_hardened.pyt` | `Plugin05_SafeContourCleaner_Pro_v5_native.pyt` | ۲ | `..._ULTIMATE.pyt` (موجود نیست) |
| ۰۶ | `Plugin06_SpringRotation_ArcMap_v4_hardened.pyt` | `Plugin06_SpringRotation_Pro_v4_native.pyt` | ۱ | — (ندارد) |
| ۰۷ | `Plugin07_BatchGridBuilder_ArcMap_v6_hardened.pyt` | `Plugin07_BatchGridBuilder_Pro_v6_native.pyt` | ۱ | `..._ArcMap27_v5_EN_Full.pyt` (موجود نیست) |

> 🔸 «موجود نیست» یعنی نام نسخه‌ی قدیمی در `PROJECT_MAP.md` ذکر شده ولی فایل آن در
> نسخه‌ی فعلی مخزن حضور ندارد. (نیازمند تأیید — به یادداشت‌های پوشه‌ی Legacy هر افزونه مراجعه کنید.)

---

## جعبه‌ابزارهای یکپارچه (Master)

| فایل | سکو | توضیح |
|------|------|-------|
| `Master_ArcMap_Carto.pyt` | ArcMap | همه‌ی افزونه‌ها زیر یک جعبه‌ابزار واحد. |
| `Master_Pro_Carto.pyt` | ArcGIS Pro | همه‌ی افزونه‌ها زیر یک جعبه‌ابزار واحد. |

---

## نام ابزارهای داخل هر افزونه

| افزونه | ابزار(ها) |
|------|-----------|
| ۰۱ | 01) Create Bridge Points · 02) Create Culvert Points · 03) Rotate Existing Bridge Points · 04) Rotate Existing Culvert Points |
| ۰۲ | Deconflict Roads vs Nearby Features (Points/Lines/Polygons) |
| ۰۳ | Optimize Contour Label Anchors · Validate Label Anchors (QA) |
| ۰۴ | Elevation Text Deconflict (2 Modes) |
| ۰۵ | AOI Brush Builder (Create / Add / Subtract) · Safe Contour Cleaner (Print-Ready) |
| ۰۶ | Spring Rotation, Final Tool (۵ روش محاسبه) |
| ۰۷ | Batch Grid Builder (دو موتور: SMART_FEATURE / ESRI_XML) |
