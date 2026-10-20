# PSPDecrypt (Windows x64)

This folder contains the unmodified `pspdecrypt.exe` supplied by the user for this private GTA Modding Studio project.

- Binary SHA-256: `6eea3ad4003718e36fb8655f74f30101553d97fe95f01ebe09c1d4f66741e756`
- File size: 3,123,447 bytes
- PE format: Windows x64 console executable
- File timestamp inside the supplied ZIP: 2021-03-05 (this is not sufficient to identify an exact upstream commit/version)
- Upstream project: https://github.com/John-K/pspdecrypt
- Upstream documentation states that PSPDecrypt is licensed under GPLv3 and supports PRX/PSP executable decryption and PSAR/PBP extraction: https://github.com/John-K/pspdecrypt/blob/master/Readme.md

The Studio invokes the utility only after an explicit user action, using separate subprocess arguments (not a shell command). It never runs a decrypted EBOOT as a program. Decrypted ELF files are analysis artifacts and must not be inserted directly into a retail PSP ISO in place of encrypted `EBOOT.BIN`.

The exact source revision corresponding to the supplied 2021 binary has not been established. The upstream repository contains the source and GPLv3 license; before redistributing this bundle outside the intended personal project, include the corresponding source version or comply with the GPL's applicable source-availability requirements.
