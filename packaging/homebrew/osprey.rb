class Osprey < Formula
  desc "Ubiquiti PTZ controller for an existing Frigate installation"
  homepage "https://github.com/grayslawson/osprey"
  version "0.1.0"
  url "https://github.com/grayslawson/osprey/archive/refs/tags/v0.1.0.tar.gz"
  # Add the archive checksum when copying this formula into a Homebrew tap.
  # The checksum is intentionally omitted here so direct installation works
  # before the first GitHub release archive exists.
  license "MIT"
  def install
    bin.install "scripts/osprey"
    bin.install "scripts/osprey-doctor.sh"
  end
  test do
    assert_match "Usage: osprey", shell_output("#{bin}/osprey --help")
  end
end
