#!/bin/bash
# =============================================================================
#  CertaOps — yerel operatör konsolunu başlatır.  macOS'ta ÇİFT TIKLAYIN.
#
#  Bu dosya BİLEREK çok kısa. macOS'un /bin/bash'i 3.2 sürümünde (2007) ve
#  modern bash'te sorunsuz çalışan bazı yapıları — örneğin komut ikamesi
#  içindeki heredoc'u — ayrıştıramıyor. Bütün mantık baslat.py içinde;
#  burada yalnızca bir Python bulup ona devretmek var. Aşağıdaki satırlar
#  Bourne shell'den beri değişmeyen yapılar; hiçbir bash sürümünde kırılmaz.
#
#  Kullanim:
#    ./baslat.command                         mod ve rolu menuden sec
#    ./baslat.command --sap --rol denetci     canli SAP, denetci
#    ./baslat.command --sim --rol satinalmaci simulasyon, satinalmaci
#    PORT=8080 ./baslat.command
#
#  Cift tiklandiginda terminal menusunden mod ve rol secilir.
# =============================================================================

cd "$(dirname "$0")" || exit 1

for PY in python3.13 python3.12 python3.11 python3.10 python3
do
  if command -v "$PY" >/dev/null 2>&1
  then
    exec "$PY" baslat.py "$@"
  fi
done

echo ""
echo "  Python 3 bulunamadi."
echo "  Kurulum:  brew install python@3.12"
echo "  veya:     https://www.python.org/downloads/"
echo ""
echo "  Kapatmak icin Enter'a basin."
read -r
exit 1
