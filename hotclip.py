"""Entry point for the packaged Clipwise.exe (also works as `python hotclip.py`)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--yt-dlp":  # used by the live-chat recorder
        import yt_dlp

        sys.exit(yt_dlp.main(sys.argv[2:]))
    from app.__main__ import main

    main()
