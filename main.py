"""Optional file entry point for hosting panels; the Docker image uses -m videobot."""

from videobot.__main__ import main

if __name__ == "__main__":
    main()
