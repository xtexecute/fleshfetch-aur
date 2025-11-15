# Maintainer: xtexecute <none@none>
pkgname=fleshfetch-aur
pkgver=1.0.0
pkgrel=1
pkgdesc="A GTK4-based clicker game written in Python with global Supabase leaderboard, written as a joke.. don't take it too seriously."
arch=('x86_64')
url="https://github.com/xtexecute/fleshfetch-aur"
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'python-requests')
source=("git+https://github.com/xtexecute/fleshfetch-aur.git#branch=main")
sha256sums=('SKIP')

package() {
    cd "$srcdir/fleshfetch-aur"

    # install
    install -Dm644 fleshfetch.py "$pkgdir/usr/share/fleshfetch/fleshfetch.py"
    install -Dm644 flesh1.png "$pkgdir/usr/share/fleshfetch/flesh1.png"
    install -Dm755 fleshfetch "$pkgdir/usr/bin/fleshfetch"

    # config
    install -Dm644 default.conf "$pkgdir/etc/fleshfetch/config"
}

# vim:set ts=2 sw=2 et:
