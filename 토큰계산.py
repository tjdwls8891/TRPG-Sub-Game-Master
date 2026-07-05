import os
import sys
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError


def main():
    """
    여러 줄의 텍스트 입력을 받아 Gemini API를 통해 토큰 수를 계산하고 출력합니다.
    """
    # 환경 변수 로드
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        print("⚠️ 환경 변수에서 GEMINI_API_KEY를 찾을 수 없습니다. .env 파일을 확인하십시오.")
        sys.exit(1)

    # API 클라이언트 초기화
    client = genai.Client(api_key=api_key)
    model_id = "gemini-3-flash-preview"  # 토큰 계산의 기준이 될 모델

    print("==================================================")
    print("📝 토큰을 계산할 텍스트를 입력하십시오.")
    print("입력을 모두 마친 후, 새로운 줄에 대문자로 'EOF'를 입력하고 엔터를 누르면 계산이 시작됩니다.")
    print("==================================================")

    input_lines = []
    while True:
        try:
            line = input()
            if line.strip() == "EOF":
                break
            input_lines.append(line)
        except EOFError:
            break

    full_text = "\n".join(input_lines).strip()

    if not full_text:
        print("⚠️ 입력된 텍스트가 없습니다. 프로그램을 종료합니다.")
        sys.exit(0)

    print("\n⏳ 토큰 수를 계산 중입니다...")

    try:
        # API를 통한 토큰 계산
        response = client.models.count_tokens(
            model=model_id,
            contents=full_text
        )

        total_tokens = response.total_tokens

        print("==================================================")
        print(f"✅ 계산 완료")
        print(f"▶ 텍스트 길이 (공백 포함): {len(full_text):,} 자")
        print(f"▶ 총 토큰 수: {total_tokens:,} 토큰")
        print("==================================================")

    except APIError as e:
        print(f"\n⚠️ API 통신 중 오류가 발생했습니다: {e}")
    except Exception as e:
        print(f"\n⚠️ 예기치 못한 오류가 발생했습니다: {e}")


if __name__ == "__main__":
    main()