class Osprey < Formula
  desc "Ubiquiti PTZ controller for an existing Frigate installation"
  homepage "https://github.com/grayslawson/osprey"
  url "https://github.com/grayslawson/osprey/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "MIT"
  def install
    bin.install "scripts/osprey"
    bin.install "scripts/osprey-doctor.sh"
  end
  test do
    assert_match "Usage: osprey", shell_output("#{bin}/osprey --help")
  end
end
