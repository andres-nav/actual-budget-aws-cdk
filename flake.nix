{
  description = "";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
    in
    {
      devShells.${system}.default =
        with import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        mkShell {
          packages = [
            nodejs
            pnpm
          ];
        };
    };
}
