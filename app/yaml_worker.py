"""Isolated parser: input is a private file, output never includes YAML text."""
import json
import sys


class ValidationMessage(ValueError):
    """Only application-authored messages may be returned verbatim."""


def main():
    if sys.platform == "linux":
        import resource
        limit = int(sys.argv[2]) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import yaml
    try:
        with open(sys.argv[1], "rb") as source:
            text = source.read().decode("utf-8-sig", errors="strict")
        if not text.strip() or "\x00" in text:
            raise ValidationMessage("文件为空或包含 NUL")
        if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise ValidationMessage("文件包含二进制控制字符")
        value = yaml.safe_load(text)
        if not isinstance(value, dict):
            raise ValidationMessage("YAML 根节点必须为 mapping（键值对象）")
        result = {"ok": True}
    except UnicodeDecodeError:
        result = {"ok": False, "message": "文件必须使用 UTF-8 编码"}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
        result = {"ok": False, "message": "YAML 语法错误" + location}
    except ValidationMessage as exc:
        result = {"ok": False, "message": str(exc)}
    except (ValueError, OverflowError):
        # SafeLoader's !!int/!!float constructors can include the original
        # scalar in Python's exception message (which may be a node password).
        result = {"ok": False, "message": "YAML 类型或数值不合法"}
    except (MemoryError, RecursionError):
        result = {"ok": False, "message": "YAML 复杂度超出资源限制"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

