# Osprey package for the current source layout (cuckoo/ + pyunifiwire/).
#
# Phase 0 (specs/002-python-package-cli) replaces this with a
# buildPythonApplication of the osprey-ptz wheel and an `osprey` entry point.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  openssl,
  ffmpeg-headless,
  src ? ./.,
}:
let
  pythonEnv = python3.withPackages (ps: [ ps.paho-mqtt ]);
in
stdenvNoCC.mkDerivation {
  pname = "osprey";
  version = lib.trim (builtins.readFile ./VERSION);

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./VERSION
      ./LICENSE
      ./NOTICE
      ./cuckoo
      ./pyunifiwire/src
      ./pyunifiwire/LICENSE
    ];
  };

  nativeBuildInputs = [ makeWrapper ];
  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    share=$out/share/osprey
    mkdir -p $share/pyunifiwire $out/bin
    cp -r cuckoo $share/cuckoo
    cp -r pyunifiwire/src $share/pyunifiwire/src
    rm -rf $share/cuckoo/tests $share/cuckoo/harness
    cp VERSION LICENSE NOTICE $share/
    cp pyunifiwire/LICENSE $share/pyunifiwire/LICENSE
    ${pythonEnv.interpreter} -m compileall -q $share/cuckoo $share/pyunifiwire/src

    # unifiwire.certs shells out to openssl; the operator preview uses ffmpeg.
    makeWrapper ${pythonEnv.interpreter} $out/bin/osprey \
      --add-flags $share/cuckoo/main.py \
      --set PYTHONPATH $share/pyunifiwire/src \
      --set PYTHONDONTWRITEBYTECODE 1 \
      --prefix PATH : ${lib.makeBinPath [ openssl ffmpeg-headless ]}
    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    $out/bin/osprey --help >/dev/null
    runHook postInstallCheck
  '';

  passthru = { inherit pythonEnv; };

  meta = {
    description = "ONVIF/RTSP controller for supported Ubiquiti PTZ cameras, for an existing Frigate installation";
    homepage = "https://github.com/grayslawson/osprey";
    license = lib.licenses.mit;
    mainProgram = "osprey";
    platforms = lib.platforms.linux;
  };
}
