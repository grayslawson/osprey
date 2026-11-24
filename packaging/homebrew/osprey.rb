class Osprey < Formula
  desc "Diagnostics launcher for the Osprey UniFi PTZ controller"
  homepage "https://github.com/grayslawson/osprey"
  version "0.9.0b6"
  url "https://github.com/grayslawson/osprey/archive/refs/tags/v0.9.0b6.tar.gz"
  sha256 "84ee4b4713e6604fe92d9ea5e189f764d1cc668e2a484e346d37b383ccf146a2"
  license "MIT"
  def install
    bin.install "scripts/osprey"
    bin.install "scripts/osprey-doctor.sh"
  end
  test do
    assert_match "Usage: osprey", shell_output("#{bin}/osprey --help")
  end
end
