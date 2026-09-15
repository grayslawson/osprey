class Osprey < Formula
  desc "Ubiquiti PTZ controller for an existing Frigate installation"
  homepage "https://github.com/grayslawson/osprey"
  version "0.1.0"
  url "https://github.com/grayslawson/osprey/archive/refs/tags/v#{version}.tar.gz"
  # A Homebrew tap should add the SHA-256 for each published release archive.
  license "MIT"

  def install
    bin.install "scripts/osprey"
    bin.install "scripts/osprey-doctor.sh"
  end

  test do
    assert_match "Usage: osprey", shell_output("#{bin}/osprey --help")
  end
end
