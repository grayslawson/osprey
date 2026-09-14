{ stdenvNoCC }:
stdenvNoCC.mkDerivation {
  pname = "osprey"; version = "0.1.0"; src = ./.; dontBuild = true;
  installPhase = ''mkdir -p $out/share/osprey; cp -r cuckoo pyunifiwire $out/share/osprey/'';
}
