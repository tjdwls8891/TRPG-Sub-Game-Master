import sys


def replace_literal_newlines(text: str) -> str:
    """
    문자열에 포함된 리터럴 '\n' (백슬래시와 n)을 실제 줄바꿈 문자로 변환합니다.
    """
    # 원시 문자열(raw string) 표현인 r'\n'을 사용하여 이스케이프 문자를 처리합니다.
    return text.replace(r'\n', '\n')


def main():
    print("문자열 변환기입니다. '\\n'이 포함된 텍스트를 입력하면 줄바꿈으로 변환하여 출력합니다.")
    print("종료하려면 'quit' 또는 'exit'를 입력하세요.\n")

    while True:
        try:
            # 사용자로부터 문자열을 입력받습니다.
            user_input = input("입력: ")

            # 종료 조건 확인
            if user_input.strip().lower() in ['quit', 'exit']:
                print("프로그램을 종료합니다.")
                break

            # 변환 및 출력
            result = replace_literal_newlines(user_input)
            print("\n[출력 결과]")
            print(result)
            print("-" * 30)

        except EOFError:
            # 입력 스트림이 끝났을 경우(예: Ctrl+D) 종료합니다.
            break
        except KeyboardInterrupt:
            # 사용자가 실행을 강제 중단했을 경우(예: Ctrl+C) 종료합니다.
            print("\n프로그램을 강제 종료합니다.")
            break


if __name__ == "__main__":
    main()